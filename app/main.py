# app/main.py
"""ASGI-точка входа; тяжёлая обработка документов выполняется отдельным worker."""

from app.observability import configure_logging
from app.runtime import configure_native_threads
from app.settings import Settings


def application():
    """Применяет лимиты потоков до первого импорта HTTP-адаптера с PDF/OCR."""
    settings = Settings()
    configure_native_threads(settings)
    configure_logging(settings.log_level)
    from app.web.api import create_app

    return create_app(settings)


app = application()
