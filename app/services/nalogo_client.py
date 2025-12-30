from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from loguru import logger

from app.config import settings

try:
    # Импортируем лениво, чтобы не падать, если зависимость не установлена,
    # при этом можно отключить фичу через NALOGO_ENABLED=false
    from nalogo import Client as NaloGOClient  # type: ignore
    from nalogo.dto.income import IncomeServiceItem  # type: ignore
except Exception:  # pragma: no cover - защитный импорт
    NaloGOClient = None
    IncomeServiceItem = None


_client_lock = asyncio.Lock()
_client: Optional["NaloGOClient"] = None


def _minor_to_rub_decimal(amount_minor: int) -> Decimal:
    rub = (Decimal(amount_minor) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return rub


async def _init_client_if_needed() -> Optional["NaloGOClient"]:
    global _client
    if not settings.nalogo_enabled:
        return None
    if NaloGOClient is None:
        logger.bind(event="nalogo.init").warning("Библиотека nalogo не установлена, отправка чеков отключена")
        return None
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        try:
            c = NaloGOClient(
                storage_path=settings.nalogo_storage_path,
                device_id=settings.nalogo_device_id,
            )
            # Аутентификация по ИНН/паролю (рекомендуется хранить в .env)
            if settings.nalogo_inn and settings.nalogo_password:
                token = await c.create_new_access_token(settings.nalogo_inn, settings.nalogo_password)
                await c.authenticate(token)
                logger.bind(event="nalogo.auth").info("NaloGO клиент аутентифицирован по ИНН/паролю")
            else:
                # Если токен уже сохранён ранее в storage_path — клиент поднимется без дополнительных шагов
                # Иначе разработчик может включить SMS-флоу вне этого кода.
                logger.bind(event="nalogo.auth").info("NaloGO клиент инициализирован (используется сохранённое состояние)")
            _client = c
            return _client
        except Exception as e:
            logger.bind(event="nalogo.init_error", error=str(e)).error("Не удалось инициализировать NaloGO клиента")
            return None


async def ensure_inited() -> None:
    await _init_client_if_needed()


async def send_income(name: str, amount_minor: int, quantity: int = 1, *, order_id: Optional[int] = None, buyer_tg_id: Optional[int] = None) -> Optional[str]:
    """
    Отправляет чек (доход) в ФНС через сервис самозанятых.
    Возвращает UUID чека (approvedReceiptUuid) или None при ошибке/отключено.
    """
    if not settings.nalogo_enabled:
        return None
    client = await _init_client_if_needed()
    if client is None or IncomeServiceItem is None:
        return None
    try:
        income_api = client.income()
        rub_amount = _minor_to_rub_decimal(amount_minor)
        item = IncomeServiceItem(
            name=name,
            amount=rub_amount,
            quantity=Decimal(str(quantity)),
        )
        result = await income_api.create(item.name, item.amount, item.quantity)
        receipt_uuid = result.get("approvedReceiptUuid") if isinstance(result, dict) else None
        logger.bind(
            event="nalogo.receipt",
            order_id=order_id,
            buyer_tg_id=buyer_tg_id,
            amount=str(rub_amount),
            receipt_uuid=receipt_uuid,
        ).info("Чек отправлен в ФНС через NaloGO")
        return receipt_uuid
    except Exception as e:
        logger.bind(
            event="nalogo.error",
            order_id=order_id,
            buyer_tg_id=buyer_tg_id,
            error=str(e),
        ).error("Ошибка отправки чека в ФНС через NaloGO")
        return None


