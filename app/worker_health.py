# app/worker_health.py
"""Лёгкая адресная проверка Celery без импорта PDF/OCR/NumPy и чужих worker."""

from socket import gethostname

from app.infrastructure.queue import make_celery
from app.runtime import configure_native_threads
from app.settings import Settings


def main() -> int:
    """Требует pong от worker именно текущего контейнера и ограничивает ожидание."""
    settings = Settings()
    configure_native_threads(settings)
    name = f"celery@{gethostname()}"
    try:
        client = make_celery(settings)
        replies = client.control.ping(
            destination=[name], timeout=settings.worker_health_timeout_seconds
        )
        return 0 if any(item.get(name, {}).get("ok") == "pong" for item in replies) else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
