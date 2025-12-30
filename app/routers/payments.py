from fastapi import APIRouter, Header, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db_session
from app.models import Order, Item, Purchase, ItemType, OrderStatus, User, ItemCode, Receipt, ReceiptStatus, ReceiptType
from aiogram import Bot
from bot.webhook_app import bot as global_bot
from app.config import settings
from app.services.delivery import DeliveryService
from app.utils.texts import load_texts
from app.services.yookassa import verify_webhook_basic, is_trusted_yookassa_ip, YooKassaClient
from app.services.nalogo_client import send_income as nalogo_send_income

router = APIRouter(prefix="/payments", tags=["payments"]) 


def get_bot() -> Bot:
    # Используем общий экземпляр бота, чтобы не открывать/не закрывать сессии на каждый запрос
    return global_bot


@router.post("/yookassa/webhook")
async def yookassa_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db_session),
    bot: Bot = Depends(get_bot),
) -> dict:
    # Верификация Basic (если включена)
    if not verify_webhook_basic(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await request.json()
    # Опциональная проверка IP (усиление безопасности)
    try:
        peer = request.client.host if request.client else None
    except Exception:
        peer = None
    if peer and not is_trusted_yookassa_ip(peer):
        # Не блокируем, просто пишем краткий лог
        from loguru import logger
        logger.bind(event="yk.webhook").info("Webhook ЮKassa от IP вне списка доверенных: {ip}", ip=peer)

    # Webhook формата YooKassa: {event, object:{id,status,amount,metadata,...}}
    obj = payload.get("object", {}) if isinstance(payload, dict) else {}
    event = payload.get("event") if isinstance(payload, dict) else None
    metadata = obj.get("metadata", {}) if isinstance(obj, dict) else {}
    status = obj.get("status")

    # Нас интересует только успешная оплата
    if not (event == "payment.succeeded" and status == "succeeded"):
        return {"ok": True}

    # Донаты: нет orderId, обрабатываем отдельно
    donation_raw = metadata.get("donation")
    donation_flag = False
    if isinstance(donation_raw, bool):
        donation_flag = donation_raw
    elif isinstance(donation_raw, str):
        donation_flag = donation_raw.strip().lower() in {"true", "1", "yes"}
    if donation_flag:
        # Отправка чека ФНС для доната (если включено) + запись в БД
        try:
            amount_value = (obj.get("amount", {}) or {}).get("value")
            buyer_tg_id = metadata.get("buyer_tg_id")
            try:
                buyer_tg_id_int = int(buyer_tg_id) if buyer_tg_id is not None and str(buyer_tg_id).isdigit() else None
            except Exception:
                buyer_tg_id_int = None
            # Преобразуем сумму в minor (копейки)
            from decimal import Decimal, ROUND_HALF_UP
            amount_minor = int((Decimal(str(amount_value or "0")) * Decimal(100)).quantize(0, rounding=ROUND_HALF_UP))
            rec = Receipt(
                type=ReceiptType.DONATION,
                order_id=None,
                buyer_tg_id=str(buyer_tg_id) if buyer_tg_id is not None else None,
                title="Донат",
                amount_minor=amount_minor,
                currency="RUB",
                status=ReceiptStatus.PENDING,
            )
            db.add(rec)
            await db.flush()
            uuid = await nalogo_send_income("Донат", amount_minor, order_id=None, buyer_tg_id=buyer_tg_id_int)
            from datetime import datetime
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
        except Exception:
            pass
        # Уведомление админа
        if settings.admin_chat_id:
            try:
                amount_value = (obj.get("amount", {}) or {}).get("value")
                buyer_tg_id = metadata.get("buyer_tg_id")
                try:
                    buyer_tg_id_int = int(buyer_tg_id) if buyer_tg_id is not None and str(buyer_tg_id).isdigit() else None
                except Exception:
                    buyer_tg_id_int = None
                buyer_username = None
                if buyer_tg_id_int is not None:
                    buyer_username = (await db.execute(select(User.username).where(User.tg_id == buyer_tg_id_int))).scalar_one_or_none()
                texts = load_texts().get("notifications", {})
                template = texts.get("donation_received") or (
                    "🎁 Донат получен\nСумма: {amount} ₽\nОт: {buyer_username}"
                )
                text = template.format(
                    amount=amount_value or "0.00",
                    buyer_username=(f"@{buyer_username}" if buyer_username else "-"),
                )
                await bot.send_message(int(settings.admin_chat_id), text)
            except Exception:
                pass
        return {"ok": True}

    # Счета, созданные вручную администратором (без orderId)
    admin_invoice_raw = metadata.get("admin_invoice")
    admin_invoice_flag = False
    if isinstance(admin_invoice_raw, bool):
        admin_invoice_flag = admin_invoice_raw
    elif isinstance(admin_invoice_raw, str):
        admin_invoice_flag = admin_invoice_raw.strip().lower() in {"true", "1", "yes"}
    if admin_invoice_flag:
        if settings.admin_chat_id:
            try:
                amount_value = (obj.get("amount", {}) or {}).get("value")
                description = obj.get("description") or "—"
                text = (
                    "🧾 Админ-счёт оплачен\n"
                    f"Сумма: {amount_value or '0.00'} ₽\n"
                    f"Описание: {description}"
                )
                await bot.send_message(int(settings.admin_chat_id), text)
            except Exception:
                pass
        return {"ok": True}

    # Покупки: ожидаем наличие paymentId и работаем с заказом
    payment_id = metadata.get("paymentId")
    if not payment_id:
        raise HTTPException(status_code=400, detail="paymentId missing")

    order = (await db.execute(select(Order).where(Order.id == int(payment_id)))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")

    # Идемпотентность: если уже paid — просто 200 OK
    if order.status == OrderStatus.PAID:
        return {"ok": True}

    order.status = "paid"

    item = (await db.execute(select(Item).where(Item.id == order.item_id))).scalar_one_or_none()
    if item:
        purchase = Purchase(order_id=order.id, user_id=order.user_id, item_id=item.id, delivery_info=None)
        db.add(purchase)

        # Попытка выдать текстовый код, если есть в наличии
        allocated_code: str | None = None
        if item.item_type == ItemType.DIGITAL and item.delivery_type == 'codes':
            code_row = (await db.execute(
                select(ItemCode).where(ItemCode.item_id == item.id, ItemCode.is_sold == False)
            )).scalars().first()
            if code_row:
                code_row.is_sold = True
                code_row.sold_order_id = order.id
                allocated_code = code_row.code

        if order.buyer_tg_id:
            delivery = DeliveryService(bot)
            try:
                if allocated_code:
                    # Отправляем код жирным (HTML)
                    text = f"<b>{allocated_code}</b>"
                    await bot.send_message(int(order.buyer_tg_id), text, reply_markup=None, parse_mode="HTML")
                    await delivery.deliver(int(order.buyer_tg_id), item)
                else:
                    await delivery.deliver(int(order.buyer_tg_id), item)
            except Exception:
                pass

    await db.commit()

    # Отправка чека ФНС (если включено) + запись в БД
    try:
        buyer_tg_id_int = None
        if order.buyer_tg_id:
            try:
                buyer_tg_id_int = int(order.buyer_tg_id)
            except Exception:
                buyer_tg_id_int = None
        title_for_receipt = item.title if item else "Покупка"
        rec = Receipt(
            type=ReceiptType.PURCHASE,
            order_id=order.id,
            buyer_tg_id=str(order.buyer_tg_id) if order.buyer_tg_id else None,
            title=title_for_receipt,
            amount_minor=order.amount_minor,
            currency="RUB",
            status=ReceiptStatus.PENDING,
        )
        db.add(rec)
        await db.flush()
        uuid = await nalogo_send_income(title_for_receipt, order.amount_minor, order_id=order.id, buyer_tg_id=buyer_tg_id_int)
        from datetime import datetime
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
    except Exception:
        pass

    if settings.admin_chat_id:
        try:
            texts = load_texts().get("notifications", {})
            template = texts.get("order_paid") or (
                "💳 Оплата получена\n"
                "Товар: {item}\nСумма: {amount} ₽\nПокупатель: {buyer} {buyer_username}\nЗаказ: {order_id}"
            )
            buyer_username = None
            if order.buyer_tg_id:
                buyer_username = (await db.execute(select(User.username).where(User.tg_id == int(order.buyer_tg_id)))).scalar_one_or_none()
            text = template.format(
                item=item.title if item else "Донат",
                amount=f"{order.amount_minor/100:.2f}",
                buyer=order.buyer_tg_id or "-",
                buyer_username=(f"@{buyer_username}" if buyer_username else ""),
                order_id=order.id,
            )
            await bot.send_message(int(settings.admin_chat_id), text)
        except Exception:
            pass

    return {"ok": True}
