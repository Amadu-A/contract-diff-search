# app/main.py
"""ASGI-точка входа; тяжёлая обработка документов выполняется отдельным worker."""

from app.observability import configure_logging
from app.settings import Settings
from app.web.api import create_app

settings = Settings()
configure_logging(settings.log_level)
app = create_app(settings)
