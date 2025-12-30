from pathlib import Path
import io
import json
import zipfile
import httpx
from typing import Optional
import contextlib

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram import Bot
from bot.webhook_app import bot as global_bot
import hmac
from aiogram.types import FSInputFile
from app.config import settings
from app.db.session import get_db_session
from app.models import Item, ItemType, Order, Purchase, User, ItemCode, Receipt
from app.models import ReceiptStatus, ReceiptType
from app.services.nalogo_client import send_income as nalogo_send_income
from app.utils.texts import load_texts

security = HTTPBasic()
router = APIRouter(prefix="/admin", tags=["admin"]) 

templates = Jinja2Templates(directory="app/admin/templates")


def ensure_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not settings.admin_username or not settings.admin_password:
        raise HTTPException(status_code=500, detail="admin credentials not configured")
    expected_user = (settings.admin_username or "").strip()
    expected_pass = (settings.admin_password or "").strip()
    provided_user = (credentials.username or "").strip()
    provided_pass = (credentials.password or "").strip()
    if not (hmac.compare_digest(provided_user, expected_user) and hmac.compare_digest(provided_pass, expected_pass)):
        # Вернём challenge, чтобы браузер показал форму логина
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic realm=admin, charset=UTF-8"})


@router.get("/logout")
async def admin_logout(_: None = Depends(ensure_auth)):
    response = RedirectResponse(url="/admin/")
    response.headers["WWW-Authenticate"] = "Basic"
    response.status_code = 401
    return response


@router.get("/")
async def admin_index(request: Request, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    stats = {
        "users": (await db.execute(select(func.count()).select_from(User))).scalar_one(),
        "items": (await db.execute(select(func.count()).select_from(Item))).scalar_one(),
        "paid_orders": (await db.execute(select(func.count()).select_from(Order).where(Order.status == 'paid'))).scalar_one(),
        "revenue": (await db.execute(select(func.coalesce(func.sum(Order.amount_minor), 0)).where(Order.status == 'paid'))).scalar_one(),
    }
    return templates.TemplateResponse("index.html", {"request": request, "stats": stats})


@router.get("/items")
async def items_list(request: Request, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth), page: int = 1, error: str | None = None):
    page_size = 10
    from sqlalchemy import func
    codes_subq = select(ItemCode.item_id, func.count().label("codes_left")).where(ItemCode.is_sold == False).group_by(ItemCode.item_id).subquery()
    stmt = select(Item, codes_subq.c.codes_left).outerjoin(codes_subq, Item.id == codes_subq.c.item_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Item.id.desc()).offset((page-1)*page_size).limit(page_size))).all()
    items = []
    for row in rows:
        item = row[0]
        setattr(item, "codes_left", row[1])
        items.append(item)
    return templates.TemplateResponse(
        "items_list.html",
        {"request": request, "items": items, "ItemType": ItemType, "page": page, "page_size": page_size, "total": total, "error": error}
    )


@router.get("/items/backup")
async def items_backup(db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    # Соберем данные о товарах без пользователей/покупок
    items = (await db.execute(select(Item))).scalars().all()
    payload = []
    files: list[tuple[str, bytes]] = []
    for it in items:
        payload.append({
            "id": it.id,
            "title": it.title,
            "description": it.description,
            "price_minor": it.price_minor,
            "item_type": it.item_type.value,
            "image_file_id": it.image_file_id,
            "image_filename": None,
            "pricing_type": it.pricing_type.value if it.pricing_type else None,
            "service_admin_contact": it.service_admin_contact,
            "delivery_type": it.delivery_type,
            "digital_file_path": it.digital_file_path,
            "github_repo_read_grant": it.github_repo_read_grant,
            "is_visible": it.is_visible,
        })
        # Положим файлы, если существуют
        for p in [it.digital_file_path]:
            if p:
                fp = Path(p)
                if fp.is_file():
                    try:
                        files.append((f"files/{fp.name}", fp.read_bytes()))
                    except Exception:
                        pass
        # Картинка карточки: попытаемся сохранить локальную/URL/Telegram картинку
        if it.image_file_id:
            try:
                img_bytes = None
                ext = ".jpg"
                # 1) Локальный путь на диске
                if Path(str(it.image_file_id)).is_file():
                    fp = Path(str(it.image_file_id))
                    img_bytes = fp.read_bytes()
                    ext = fp.suffix.lower() or ".jpg"
                # 2) Прямая ссылка
                elif str(it.image_file_id).startswith("http"):
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(str(it.image_file_id))
                        if resp.status_code == 200:
                            img_bytes = resp.content
                            ct = resp.headers.get("content-type", "image/jpeg")
                            if "png" in ct:
                                ext = ".png"
                # 3) Telegram file_id
                else:
                    # getFile через HTTP API
                    async with httpx.AsyncClient() as client:
                        r = await client.get(f"https://api.telegram.org/bot{settings.bot_token}/getFile", params={"file_id": it.image_file_id})
                        data = r.json()
                        file_path = (data.get("result") or {}).get("file_path")
                        if file_path:
                            fr = await client.get(f"https://api.telegram.org/file/bot{settings.bot_token}/{file_path}")
                            if fr.status_code == 200:
                                img_bytes = fr.content
                                if file_path.endswith(".png"):
                                    ext = ".png"
                if img_bytes:
                    name = f"images/item_{it.id}{ext}"
                    files.append((name, img_bytes))
                    payload[-1]["image_filename"] = name
            except Exception:
                pass
    # Упакуем в ZIP: data.json + files/
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps({"items": payload}, ensure_ascii=False, indent=2))
        for name, content in files:
            z.writestr(name, content)
    mem.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(mem, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=items_backup.zip"})


@router.post("/items/restore")
async def items_restore(file: UploadFile | None = File(None), db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    # Читаем ZIP и восстанавливаем товары (без пользователей/покупок)
    if not file or not getattr(file, "filename", None):
        return RedirectResponse(url="/admin/items?error=Не выбран файл бекапа (.zip)", status_code=303)
    try:
        blob = await file.read()
        mem = io.BytesIO(blob)
        z = zipfile.ZipFile(mem)
    except Exception:
        return RedirectResponse(url="/admin/items?error=Некорректный файл: нужен .zip бекап", status_code=303)
    with z:
        data = json.loads(z.read("data.json"))
        items = data.get("items", [])
        base_dir = Path(settings.upload_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        # Восстанавливаем файлы
        for n in z.namelist():
            if n.startswith("files/") and not n.endswith("/"):
                target = base_dir / Path(n).name
                with open(target, "wb") as f:
                    f.write(z.read(n))
        # Пересоздаём записи (без конфликтов id)
        for it in items:
            rec = Item(
                title=it["title"],
                description=it["description"],
                price_minor=int(it["price_minor"]),
                item_type=ItemType(it["item_type"]),
                image_file_id=None,
                pricing_type=None,
                service_admin_contact=it.get("service_admin_contact"),
                delivery_type=it.get("delivery_type"),
                digital_file_path=str(base_dir / Path(it["digital_file_path"]).name) if it.get("digital_file_path") else None,
                github_repo_read_grant=it.get("github_repo_read_grant"),
                is_visible=bool(it.get("is_visible", True)),
            )
            db.add(rec)
        await db.flush()
        # Второй проход: восстановим изображения, загрузив их в Telegram для получения file_id
        # для этого нам нужно соответствие: items в том же порядке, как создавали
        created = (await db.execute(select(Item).order_by(Item.id.desc()).limit(len(items)))).scalars().all()
        created = list(reversed(created)) if created else []
        for idx, it in enumerate(items):
            img_name = it.get("image_filename")
            if img_name and idx < len(created):
                try:
                    img_target = base_dir / Path(img_name).name
                    with open(img_target, "wb") as f:
                        f.write(z.read(img_name))
                    file_id = await send_image_and_get_file_id(str(img_target))
                    if file_id:
                        created[idx].image_file_id = file_id
                except Exception:
                    pass
        await db.commit()
    return RedirectResponse(url="/admin/items", status_code=303)


@router.get("/items/new")
async def items_new(request: Request, _: None = Depends(ensure_auth)):
    return templates.TemplateResponse("item_form.html", {"request": request, "item": None, "ItemType": ItemType})


async def send_image_and_get_file_id(image_path: str) -> str:
    """Отправить изображение в Telegram-чат и получить file_id"""
    bot = global_bot
    chat_id = int(settings.admin_chat_id) if settings.admin_chat_id else None
    if not chat_id:
        return None
    input_file = FSInputFile(image_path)
    msg = await bot.send_photo(chat_id, photo=input_file, caption="Тестовое изображение для получения file_id")
    return msg.photo[-1].file_id if msg.photo else None


@router.post("/items/new")
async def items_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    price_minor: int = Form(...),  # Цена в рублях, конвертируем в копейки
    item_type: ItemType = Form(...),
    # Общие поля
    image: Optional[UploadFile] = File(None),
    # Поля для услуг
    pricing_type: Optional[str] = Form(None),
    # Поля для цифровых товаров
    delivery_type: Optional[str] = Form(None),
    digital_file: Optional[UploadFile] = File(None),
    github_repo_read_grant: Optional[str] = Form(None),
    # Поля для модулей
    module_name: Optional[str] = Form(None),
    module_file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(ensure_auth),
):
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Обработка общего изображения
    image_file_id = None
    if image and getattr(image, "filename", None):
        image_path = upload_dir / image.filename
        with open(image_path, "wb") as f:
            f.write(await image.read())
        # Получаем file_id через Telegram; если не удалось — используем локальный путь
        try:
            image_file_id = await send_image_and_get_file_id(str(image_path))
        except Exception:
            image_file_id = str(image_path)

    # Обработка специфичных файлов
    digital_file_path = None
    if item_type == ItemType.DIGITAL and delivery_type == 'file' and digital_file and getattr(digital_file, "filename", None):
        digital_file_path = str(upload_dir / digital_file.filename)
        with open(digital_file_path, "wb") as f:
            f.write(await digital_file.read())
    # поля для модулей удалены
            
    # Создаем новый товар
    # Подставим дефолтную картинку, если не загружена
    if not image_file_id:
        defaults = load_texts().get("defaults", {}).get("images", {})
        if item_type == ItemType.SERVICE:
            image_file_id = defaults.get("service")
        elif item_type == ItemType.DIGITAL:
            image_file_id = defaults.get("digital")

    item = Item(
        title=title,
        description=description,
        price_minor=price_minor * 100,  # Конвертируем рубли в копейки
        item_type=item_type,
        image_file_id=image_file_id,
    )

    # Добавляем специфичные поля в зависимости от типа товара
    if item_type == ItemType.SERVICE:
        # Обновляем только если поле передано (позволяет менять цену без выбора заново)
        if pricing_type is not None:
            item.pricing_type = pricing_type
        item.service_admin_contact = settings.admin_username  # Берем из env
    
    elif item_type == ItemType.DIGITAL:
        item.delivery_type = delivery_type
        if delivery_type == 'file':
            item.digital_file_path = digital_file_path
        else:  # github
            item.github_repo_read_grant = github_repo_read_grant
    
    

    db.add(item)
    await db.commit()

    return RedirectResponse(url="/admin/items", status_code=302)


@router.get("/items/{item_id}")
async def items_edit(request: Request, item_id: int, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    item = (await db.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    return templates.TemplateResponse("item_form.html", {"request": request, "item": item, "ItemType": ItemType})


@router.post("/items/{item_id}")
async def items_update(
    request: Request,
    item_id: int,
    title: str = Form(...),
    description: str = Form(...),
    price_minor: int = Form(...),
    item_type: ItemType = Form(...),
    # Общие поля
    image: Optional[UploadFile] = File(None),
    # Поля для услуг
    pricing_type: Optional[str] = Form(None),
    # Поля для цифровых товаров
    delivery_type: Optional[str] = Form(None),
    digital_file: Optional[UploadFile] = File(None),
    github_repo_read_grant: Optional[str] = Form(None),
    codes_file: Optional[UploadFile] = File(None),
    # Поля для модулей
    module_name: Optional[str] = Form(None),
    module_file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(ensure_auth),
):
    item = (await db.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Обработка общего изображения
    if image and getattr(image, "filename", None):
        image_path = str(upload_dir / image.filename)
        with open(image_path, "wb") as f:
            f.write(await image.read())
        # Получаем file_id через Telegram; если не удалось — используем локальный путь
        try:
            item.image_file_id = await send_image_and_get_file_id(image_path)
        except Exception:
            item.image_file_id = image_path

    # Обработка файлов для цифровых товаров
    digital_file_path = None
    if item_type == ItemType.DIGITAL and delivery_type == 'file' and digital_file and getattr(digital_file, "filename", None):
        digital_file_path = str(upload_dir / digital_file.filename)
        with open(digital_file_path, "wb") as f:
            f.write(await digital_file.read())
    # Загрузка кодов из txt (по одной строке)
    if item_type == ItemType.DIGITAL and delivery_type == 'codes' and codes_file and getattr(codes_file, "filename", None):
        try:
            content = (await codes_file.read()).decode("utf-8")
            codes = [line.strip() for line in content.splitlines() if line.strip()]
            for c in codes:
                db.add(ItemCode(item_id=item.id, code=c))
        except Exception:
            pass
    # загрузка модулей удалена

    # Если картинка отсутствует и не была обновлена — поставим дефолт
    if not item.image_file_id:
        defaults = load_texts().get("defaults", {}).get("images", {})
        if item_type == ItemType.SERVICE:
            item.image_file_id = defaults.get("service")
        elif item_type == ItemType.DIGITAL:
            item.image_file_id = defaults.get("digital")

    # Обновляем общие поля товара
    item.title = title
    item.description = description
    item.price_minor = price_minor * 100  # Конвертируем рубли в копейки
    item.item_type = item_type
    
    # Обновляем специфичные поля в зависимости от типа товара
    if item_type == ItemType.SERVICE:
        item.pricing_type = pricing_type
        item.service_admin_contact = settings.admin_username  # Берем из env
    
    elif item_type == ItemType.DIGITAL:
        item.delivery_type = delivery_type
        if delivery_type == 'file':
            # Обновляем путь к файлу только если загружен новый файл
            if digital_file_path:
                item.digital_file_path = digital_file_path
        else:  # github
            # Обновляем репозиторий только если поле передано (может быть пустым для сохранения текущего)
            if github_repo_read_grant is not None:
                item.github_repo_read_grant = github_repo_read_grant
    
    

    await db.commit()
    return RedirectResponse(url="/admin/items", status_code=303)


@router.post("/admin/items/{item_id}/add_codes")
async def add_codes(item_id: int, file: UploadFile = File(...), db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    item = (await db.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if not item or item.item_type != ItemType.DIGITAL:
        return JSONResponse({"ok": False, "error": "Неверный товар или тип."}, status_code=400)
    try:
        content = (await file.read()).decode("utf-8")
        codes = [line.strip() for line in content.splitlines() if line.strip()]
        for c in codes:
            db.add(ItemCode(item_id=item.id, code=c))
        await db.commit()
        return JSONResponse({"ok": True, "added": len(codes)})
    except Exception:
        return JSONResponse({"ok": False, "error": "Ошибка обработки файла."}, status_code=500)


# upload_zip удалён


@router.get("/orders")
async def orders_list(request: Request, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth), page: int = 1, q: str | None = None):
    page_size = 10
    stmt = select(Order).where(Order.item_id.is_not(None))
    if q:
        try:
            stmt = stmt.where(Order.buyer_tg_id == str(int(q)))
        except Exception:
            stmt = stmt.where(Order.buyer_tg_id == "__no__match__")
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    orders = (await db.execute(stmt.order_by(Order.id.desc()).offset((page-1)*page_size).limit(page_size))).scalars().all()
    return templates.TemplateResponse("orders_list.html", {"request": request, "orders": orders, "page": page, "page_size": page_size, "total": total, "query": q})


@router.post("/orders/{order_id}/delete")
async def orders_delete(order_id: int, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    # Удалим связанные покупки (на случай отсутствия каскада в БД)
    await db.execute(delete(Purchase).where(Purchase.order_id == order_id))
    await db.delete(order)
    await db.commit()
    return RedirectResponse(url="/admin/orders", status_code=303)


@router.post("/orders/delete")
async def orders_delete_form(order_id: int = Form(...), db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    order = (await db.execute(select(Order).where(Order.id == int(order_id)))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    await db.execute(delete(Purchase).where(Purchase.order_id == int(order_id)))
    await db.delete(order)
    await db.commit()
    return RedirectResponse(url="/admin/orders", status_code=303)


@router.get("/users")
async def users_list(request: Request, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth), page: int = 1, q: str | None = None):
    page_size = 10
    stmt = select(User)
    if q:
        try:
            tg = int(q)
            stmt = stmt.where(User.tg_id == tg)
        except Exception:
            stmt = stmt.where(User.tg_id == -1)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    users = (await db.execute(stmt.order_by(User.id.desc()).offset((page-1)*page_size).limit(page_size))).scalars().all()
    return templates.TemplateResponse("users_list.html", {"request": request, "users": users, "page": page, "page_size": page_size, "total": total, "query": q})


@router.post("/items/{item_id}/delete")
async def items_delete(item_id: int, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    item = (await db.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    # Удалим связанные локальные файлы (если они существуют на диске)
    try:
        # Картинка: удаляем, только если это локальный путь (не Telegram file_id и не URL)
        if item.image_file_id and not str(item.image_file_id).startswith("http") and not str(item.image_file_id).startswith("AgAC"):
            p = Path(str(item.image_file_id))
            if p.is_file():
                with contextlib.suppress(Exception):
                    p.unlink()
        # Файл цифрового товара
        if item.item_type == ItemType.DIGITAL and item.delivery_type == 'file' and item.digital_file_path:
            p = Path(str(item.digital_file_path))
            if p.is_file():
                with contextlib.suppress(Exception):
                    p.unlink()
        # ZIP модулей более не используется
    except Exception:
        pass
    await db.delete(item)
    await db.commit()
    return RedirectResponse(url="/admin/items", status_code=303)


# Чеки ФНС
@router.get("/receipts")
async def receipts_list(request: Request, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth), status: str | None = None, page: int = 1):
    page_size = 20
    stmt = select(Receipt)
    if status and status not in {"all", ""}:
        stmt = stmt.where(Receipt.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    receipts = (await db.execute(stmt.order_by(Receipt.id.desc()).offset((page-1)*page_size).limit(page_size))).scalars().all()
    return templates.TemplateResponse("receipts_list.html", {"request": request, "receipts": receipts, "page": page, "page_size": page_size, "total": total, "status": status or "all"})


@router.post("/receipts/{receipt_id}/resend")
async def receipts_resend(receipt_id: int, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    from datetime import datetime
    rec = (await db.execute(select(Receipt).where(Receipt.id == receipt_id))).scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Receipt not found")
    buyer_tg_id_int = None
    if rec.buyer_tg_id:
        try:
            buyer_tg_id_int = int(rec.buyer_tg_id)
        except Exception:
            buyer_tg_id_int = None
    uuid = await nalogo_send_income(rec.title, rec.amount_minor, order_id=rec.order_id, buyer_tg_id=buyer_tg_id_int)
    rec.attempts_count = (rec.attempts_count or 0) + 1
    rec.last_attempt_at = datetime.utcnow()
    if uuid:
        rec.status = ReceiptStatus.ACCEPTED
        rec.fns_id = uuid
        rec.error_text = None
    else:
        rec.status = ReceiptStatus.FAILED
    db.add(rec)
    await db.commit()
    return RedirectResponse(url="/admin/receipts", status_code=303)


# Переключение видимости товара
@router.post("/items/{item_id}/toggle_visibility")
async def items_toggle_visibility(item_id: int, db: AsyncSession = Depends(get_db_session), _: None = Depends(ensure_auth)):
    item = (await db.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item.is_visible = not item.is_visible
    db.add(item)
    await db.commit()
    return RedirectResponse(url="/admin/items", status_code=303)
