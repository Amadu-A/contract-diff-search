# tests/unit/test_observability.py
"""Единый timing-декоратор: sync/async успех, ошибка, метаданные и безопасный JSON."""

import asyncio
import json
import logging

import pytest

from app.observability import JsonFormatter, timed


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_timing_records_once_without_sensitive_arguments(caplog, asynchronous, fails):
    """Операция пишет одно измерение даже при исключении; аргумент документа не журналируется."""

    @timed("synthetic_operation")
    def operation(secret: str):
        """Представляет измеряемый сценарий без внешних зависимостей."""
        if fails:
            raise ValueError(secret)
        return 42

    @timed("synthetic_operation")
    async def async_operation(secret: str):
        """Проверяет тот же контракт для асинхронной операции."""
        if fails:
            raise ValueError(secret)
        return 42

    selected = async_operation if asynchronous else operation
    with caplog.at_level(logging.INFO):
        if fails:
            with pytest.raises(ValueError):
                if asynchronous:
                    asyncio.run(selected("private-document-value"))
                else:
                    selected("private-document-value")
        else:
            result = (
                asyncio.run(selected("private-document-value"))
                if asynchronous
                else selected("private-document-value")
            )
            assert result == 42
    records = [r for r in caplog.records if r.getMessage() == "operation_timing"]
    assert len(records) == 1
    record = records[0]
    assert record.duration_ms >= 0
    assert record.status == ("error" if fails else "success")
    formatted = JsonFormatter().format(record)
    assert "private-document-value" not in formatted
    assert json.loads(formatted)["operation"] == "synthetic_operation"
    assert selected.__name__ == ("async_operation" if asynchronous else "operation")
