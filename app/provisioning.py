# app/provisioning.py
"""Закрытый протокол настроек для однокомандной подготовки существующего брокера.

Вывод содержит пароль и предназначен только для захвата scripts/up.sh в памяти.
Этот процесс использует тот же Pydantic Settings, что API и worker, без shell-source.
"""

import re
import sys
from ipaddress import ip_address

from pydantic import ValidationError

from app.settings import Settings


def bootstrap_values(settings: Settings) -> tuple[str, str, str, str, str, str]:
    """Проверяет учётную запись и возвращает настройки с пределом ожидания CLI."""
    container, user, vhost = (
        settings.rabbitmq_container,
        settings.rabbitmq_user,
        settings.rabbitmq_vhost,
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise ValueError("RABBITMQ_CONTAINER: укажите имя существующего контейнера RabbitMQ")
    for name, value in (("RABBITMQ_USER", user), ("RABBITMQ_VHOST", vhost)):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise ValueError(f"{name}: требуется отдельное имя из букв, цифр, точки, дефиса или _")
    if user.lower() in {"guest", "admin", "administrator", "shared_admin"}:
        raise ValueError(
            "Для сервиса нужен отдельный пользователь RabbitMQ без административных прав"
        )
    password = settings.rabbitmq_password.get_secret_value()
    if not password or len(password.encode("utf-8")) > 1024 or any(c in password for c in "\r\n\0"):
        raise ValueError("Запишите непустой однострочный RABBITMQ_PASSWORD в .env")
    try:
        host = str(ip_address(settings.app_bind_ip))
    except ValueError:
        raise ValueError("APP_BIND_IP должен содержать IP-адрес публикации сервиса") from None
    if host in {"0.0.0.0", "::"}:
        host = "localhost"
    if ":" in host:
        host = f"[{host}]"
    return (
        container,
        user,
        vhost,
        password,
        str(settings.rabbitmq_cli_timeout_seconds),
        f"http://{host}:{settings.app_port}",
    )


def main() -> int:
    """Передаёт настройки родительскому скрипту, не раскрывая значения при ошибке валидации."""
    try:
        values = bootstrap_values(Settings())
    except ValidationError:
        print("Некорректные настройки: проверьте .env.example, .env и окружение", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write("\n".join(values) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
