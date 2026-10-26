# tests/unit/test_provisioning.py
"""Проверки Pydantic-конфигурации подготовки общего брокера и закрытого протокола."""

import pytest
from pydantic import SecretStr

from app import provisioning
from app.settings import Settings


def test_bootstrap_settings_layers_and_literal_secret(tmp_path, monkeypatch):
    """Baseline и приватный dotenv читаются как данные; process environment имеет приоритет."""
    for key in ("RABBITMQ_USER", "RABBITMQ_VHOST", "RABBITMQ_PASSWORD", "RABBITMQ_CONTAINER"):
        monkeypatch.delenv(key, raising=False)
    baseline, private = tmp_path / "baseline", tmp_path / "private"
    baseline.write_text(
        "RABBITMQ_USER=contract-diff\nRABBITMQ_VHOST=contract-diff\n", encoding="utf-8"
    )
    private.write_text("RABBITMQ_PASSWORD='literal-$(whoami)-$HOME-#-! value'\n", encoding="utf-8")
    monkeypatch.setenv("RABBITMQ_CONTAINER", "shared-rabbitmq-test-1")
    settings = Settings(_env_file=(baseline, private))
    assert provisioning.bootstrap_values(settings) == (
        "shared-rabbitmq-test-1",
        "contract-diff",
        "contract-diff",
        "literal-$(whoami)-$HOME-#-! value",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rabbitmq_container", "--help"),
        ("rabbitmq_container", "container\nother"),
        ("rabbitmq_user", "guest"),
        ("rabbitmq_user", "shared_admin"),
        ("rabbitmq_user", "bad user"),
        ("rabbitmq_vhost", "/"),
        ("rabbitmq_vhost", "--help"),
        ("rabbitmq_password", ""),
        ("rabbitmq_password", "secret\nsecond-line"),
        ("rabbitmq_password", "secret\rsecond-line"),
        ("rabbitmq_password", "secret\0tail"),
        ("rabbitmq_password", "x" * 1025),
    ],
)
def test_unsafe_bootstrap_values_rejected_without_echo(field, value):
    """Не допускает общую учётную запись, CLI-инъекции и разрыв строкового протокола."""
    values = {"rabbitmq_password": SecretStr("synthetic-safe-password"), field: value}
    settings = Settings(_env_file=None, **values)
    with pytest.raises(ValueError) as error:
        provisioning.bootstrap_values(settings)
    assert value not in str(error.value) if value else True


def test_bootstrap_cli_protocol_and_safe_failure(monkeypatch, capsys):
    """Успех выводит только закрытые строки, а отказ не раскрывает пароль или traceback."""
    settings = Settings(_env_file=None, rabbitmq_password="synthetic-$-#-secret")
    monkeypatch.setattr(provisioning, "Settings", lambda: settings)
    assert provisioning.main() == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == list(provisioning.bootstrap_values(settings))
    assert captured.err == ""
    settings.rabbitmq_password = SecretStr("")
    assert provisioning.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RABBITMQ_PASSWORD" in captured.err
    assert "synthetic-$-#-secret" not in captured.err


def test_bootstrap_validation_error_hides_inputs(monkeypatch, capsys):
    """Ошибки типов Pydantic не печатают потенциально секретное ошибочное значение."""

    def invalid_settings():
        """Создаёт настоящую ошибку валидации с чувствительным тестовым значением."""
        return Settings(_env_file=None, app_port="synthetic-private-input")

    monkeypatch.setattr(provisioning, "Settings", invalid_settings)
    assert provisioning.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "synthetic-private-input" not in captured.err
    assert "Некорректные настройки" in captured.err
