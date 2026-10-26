# app/infrastructure/processes.py
"""Ограниченное выполнение OCR/LibreOffice с отменой и завершением дерева процессов."""

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from app.domain.models import DocumentError


def run_process(
    args: list[str], cwd: Path, timeout: float, checkpoint: Callable[[str], None], stage: str
) -> bytes:
    """Запускает без shell; вывод ограничен файлом и не попадает в application log."""
    log = cwd / "process.log"
    kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    started = time.monotonic()
    try:
        with log.open("wb") as output:
            process = subprocess.Popen(
                args, cwd=cwd, stdout=output, stderr=output, stdin=subprocess.DEVNULL, **kwargs
            )
            try:
                while process.poll() is None:
                    checkpoint(stage)
                    if time.monotonic() - started > timeout:
                        raise DocumentError("Превышено время обработки документа")
                    if log.stat().st_size > 2_000_000:
                        raise DocumentError("Обработчик документа вернул слишком большой журнал")
                    time.sleep(0.2)
                if process.returncode:
                    raise DocumentError("Не удалось обработать документ внешним инструментом")
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
        return log.read_bytes()
    except FileNotFoundError as exc:
        raise DocumentError("Не установлен обязательный инструмент OCR или LibreOffice") from exc
