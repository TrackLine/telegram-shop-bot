from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import settings


def create_app() -> FastAPI:
    app = FastAPI(title="Shop Bot API")
    # Routers
    from app.routers.health import router as health_router  # local import to avoid circular deps
    from app.routers.payments import router as payments_router
    from app.routers.orders import router as orders_router
    from app.routers.admin import router as admin_router
    from bot.webhook_app import api_router as tg_router, setup_webhook, delete_webhook

    app.include_router(health_router)
    app.include_router(payments_router)
    app.include_router(orders_router)
    app.include_router(admin_router)
    app.include_router(tg_router)

    # Static uploads
    app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

    @app.on_event("startup")
    async def _on_startup() -> None:
        # Initialize database tables
        try:
            from app.db.init_db import init_db
            await init_db()
            logger.bind(event="db_init").info("Database initialization completed")
        except Exception as e:
            logger.bind(event="db_init_error", error=str(e)).error("Ошибка инициализации базы данных")
            # Не прерываем запуск приложения, но логируем ошибку
        
        # Инициализация клиента «Мой Налог», если включено
        try:
            from app.services.nalogo_client import ensure_inited
            await ensure_inited()
        except Exception as e:
            logger.bind(event="nalogo.init_error", error=str(e)).warning("Не удалось инициализировать NaloGO на старте")

        await setup_webhook()
        logger.bind(event="webhook_setup").info("Webhook configured", url=settings.webhook_url)

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        await delete_webhook()
        logger.bind(event="webhook_delete").info("Webhook removed")

    return app


app = create_app()


@logger.catch
def _startup_log() -> None:
    logger.bind(event="startup").info("FastAPI started", base_url=settings.base_url)

# Run with: uvicorn app.main:app --reload
