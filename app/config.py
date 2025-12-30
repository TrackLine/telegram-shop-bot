from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path


class Settings(BaseSettings):
    # Telegram
    bot_token: str = Field(alias="BOT_TOKEN")

    # Database
    database_url: str = Field(alias="DATABASE_URL")

    # YooKassa
    yk_shop_id: str = Field(alias="YK_SHOP_ID")
    yk_secret_key: str = Field(alias="YK_SECRET_KEY")
    yk_return_url: str = Field(alias="YK_RETURN_URL")
    yk_webhook_user: str | None = Field(alias="YK_WEBHOOK_USER", default=None)
    yk_webhook_password: str | None = Field(alias="YK_WEBHOOK_PASSWORD", default=None)

    # Server
    server_ip: str = Field(alias="SERVER_IP", default="127.0.0.1")
    base_url: str = Field(alias="BASE_URL", default="http://localhost:8000")
    port: int = Field(alias="PORT", default=8000)

    # Admin defaults
    admin_username: str = Field(alias="ADMIN_USERNAME", default="")
    admin_password: str = Field(alias="ADMIN_PASSWORD", default="")
    admin_chat_id: str | None = Field(alias="ADMIN_CHAT_ID", default=None)
    admin_tg_username: str | None = Field(alias="ADMIN_TG_USERNAME", default=None)
    show_contact_button: bool = Field(alias="SHOW_CONTACT_BUTTON", default=True)
    contact_admin: str | None = Field(alias="CONTACT_ADMIN", default=None)
    show_donate_button: bool = Field(alias="SHOW_DONATE_BUTTON", default=True)

    # Email fallback
    email_domain: str = Field(alias="EMAIL_DOMAIN", default="tg.local")

    # Uploads
    upload_dir: str = Field(alias="UPLOAD_DIR", default="uploads")

    # Webhook
    webhook_url: str | None = Field(alias="WEBHOOK_URL", default=None)
    webhook_secret: str | None = Field(alias="WEBHOOK_SECRET", default=None)

    

    # Donate
    donate_amounts: str | None = Field(alias="DONATE_AMOUNTS", default=None)

    # NaloGO (Мой Налог) — отправка чеков
    nalogo_enabled: bool = Field(alias="NALOGO_ENABLED", default=False)
    nalogo_inn: str | None = Field(alias="NALOGO_INN", default=None)
    nalogo_password: str | None = Field(alias="NALOGO_PASSWORD", default=None)
    nalogo_storage_path: str = Field(alias="NALOGO_STORAGE_PATH", default="nalogo_tokens.json")
    nalogo_device_id: str = Field(alias="NALOGO_DEVICE_ID", default="tg-shop-bot")

    # Абсолютный путь к .env относительно корня проекта
    _env_path = (Path(__file__).resolve().parents[1] / ".env").as_posix()
    model_config = SettingsConfigDict(env_file=_env_path, env_file_encoding="utf-8", extra="ignore")


settings = Settings()  # type: ignore
