# app/observability.py
"""Единый JSON-журнал и измерение значимых операций без содержимого документов."""

import inspect
import json
import logging
import os
from datetime import UTC, datetime
from functools import wraps
from time import perf_counter


def peak_memory_mb() -> float | None:
    """Возвращает пик RSS процесса на Linux; Windows не выдаёт нулевое измерение за реальный пик."""
    if os.name == "nt":
        return None
    import resource

    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)


class JsonFormatter(logging.Formatter):
    """Выводит разрешённые технические поля и исключает произвольные payload."""

    def format(self, record: logging.LogRecord) -> str:
        """Формирует одну JSON-запись без аргументов запросов и реквизитов."""
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "service": "contract-diff",
        }
        for key in (
            "operation",
            "duration_ms",
            "status",
            "job_id",
            "stage",
            "elements",
            "unverified",
            "peak_memory_mb",
        ):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Настраивает stdout; ротация контейнерных журналов принадлежит Docker."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def timed(operation: str):
    """Измеряет sync/async use-case монотонными часами; traceback не дублируется."""

    def decorate(function):
        """Сохраняет сигнатуру обёрнутой операции и выбирает вид исполнения."""

        def emit(started: float, status: str) -> None:
            """Записывает итог вызова без параметров и возвращаемых документов."""
            logging.getLogger(function.__module__).info(
                "operation_timing",
                extra={
                    "operation": operation,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                    "status": status,
                    "peak_memory_mb": peak_memory_mb(),
                },
            )

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args, **kwargs):
                """Измеряет асинхронную операцию с единственным итоговым событием."""
                started, status = perf_counter(), "success"
                try:
                    return await function(*args, **kwargs)
                except Exception:
                    status = "error"
                    raise
                finally:
                    emit(started, status)

            return async_wrapper

        @wraps(function)
        def sync_wrapper(*args, **kwargs):
            """Измеряет синхронную операцию с единственным итоговым событием."""
            started, status = perf_counter(), "success"
            try:
                return function(*args, **kwargs)
            except Exception:
                status = "error"
                raise
            finally:
                emit(started, status)

        return sync_wrapper

    return decorate
