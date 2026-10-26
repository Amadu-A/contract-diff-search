# app/__main__.py
"""Запускает HTTP с валидированными host/port из единой модели настроек."""

import uvicorn

from app.settings import Settings


def main() -> None:
    """Использует settings вместо самостоятельного чтения переменных окружения."""
    settings = Settings()
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, access_log=False)


if __name__ == "__main__":
    main()
