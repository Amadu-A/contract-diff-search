# app/maintenance.py
"""Независимая от браузера и RabbitMQ очистка временных файлов по TTL."""

import signal
from threading import Event

from app.bootstrap import build_service
from app.observability import configure_logging
from app.settings import Settings


def main() -> None:
    """Выполняет очистку после рестарта и периодически до сигнала остановки."""
    settings, stopped = Settings(), Event()
    configure_logging(settings.log_level)
    service = build_service(settings)

    def stop(*_args) -> None:
        """Завершает цикл при штатной остановке контейнера."""
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped.is_set():
        service.cleanup()
        stopped.wait(settings.cleanup_interval_seconds)


if __name__ == "__main__":
    main()
