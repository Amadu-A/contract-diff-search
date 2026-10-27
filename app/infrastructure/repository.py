# app/infrastructure/repository.py
"""Временные метаданные SQLite с WAL, квотами и атомарной арендой worker."""

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from app.domain.models import ComparisonJob, DocumentError


class SqliteJobRepository:
    """Изолирует SQL; каждый вызов имеет собственное короткое соединение."""

    def __init__(self, path: Path):
        """Создаёт таблицу без хранения текстов документов и capability-токенов."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, expires REAL NOT NULL, use_ai INTEGER NOT NULL,
                old_name TEXT NOT NULL, new_name TEXT NOT NULL, error TEXT NOT NULL, lease TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_expires ON jobs(expires)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            for name, definition in (
                ("ai_full", "INTEGER NOT NULL DEFAULT 0"),
                ("ai_unverified", "INTEGER NOT NULL DEFAULT 0"),
                ("ai_progress", "TEXT NOT NULL DEFAULT '{}'"),
                ("resume_ai", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
                    if name == "ai_full":
                        connection.execute("UPDATE jobs SET ai_full=use_ai")

    @contextmanager
    def connection(self):
        """Ограничивает ожидание блокировки и гарантирует закрытие соединения."""
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, job: ComparisonJob, max_jobs: int, max_session_jobs: int) -> None:
        """Атомарно проверяет квоту и резервирует место перед приёмом файлов."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute("SELECT COUNT(*) FROM jobs WHERE status != 'expired'").fetchone()[0]
            owned = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? AND status IN ('queued','processing')",
                (job.owner,),
            ).fetchone()[0]
            if total >= max_jobs or owned >= max_session_jobs:
                raise DocumentError(
                    "Достигнут лимит заданий; дождитесь завершения или удаления предыдущих"
                )
            data = asdict(job)
            data["ai_progress"] = json.dumps(data["ai_progress"])
            db.execute(
                f"INSERT INTO jobs ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                tuple(data.values()),
            )

    def get(self, job_id: str) -> ComparisonJob | None:
        """Возвращает запись; решение о доступе принимает прикладной слой."""
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            values = dict(row)
            values["ai_progress"] = json.loads(values["ai_progress"])
            return ComparisonJob(**values)
        return None

    def claim(self, job_id: str, lease: str) -> bool:
        """Одна доставка получает аренду; повторная не запускает второй конвейер."""
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='processing', stage='Извлечение', lease=?, updated=? WHERE id=? AND status='queued'",
                (lease, time.time(), job_id),
            )
            return cursor.rowcount == 1

    def update(self, job_id: str, **changes) -> None:
        """Обновляет только известные поля; динамические имена извне не принимаются."""
        if not changes or not set(changes) <= {
            "status",
            "stage",
            "updated",
            "expires",
            "error",
            "lease",
            "ai_progress",
        }:
            raise ValueError("Недопустимое обновление задания")
        changes["updated"] = time.time()
        if "ai_progress" in changes:
            changes["ai_progress"] = json.dumps(changes["ai_progress"])
        with self.connection() as db:
            db.execute(
                f"UPDATE jobs SET {','.join(f'{k}=?' for k in changes)} WHERE id=?",
                (*changes.values(), job_id),
            )
            if changes.get("status") == "expired":
                db.execute(
                    "UPDATE jobs SET old_name='', new_name='', error='' WHERE id=?", (job_id,)
                )

    def finish(self, job_id: str, lease: str, status: str, expires: float, error: str = "") -> bool:
        """Не позволяет позднему результату перезаписать отменённое задание."""
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET status=?, stage=?, expires=?, updated=?, error=? WHERE id=? AND lease=? AND status='processing'",
                (status, "Завершено", expires, time.time(), error, job_id, lease),
            )
            return cursor.rowcount == 1

    def due(self, now: float, stale_before: float) -> list[ComparisonJob]:
        """Находит TTL и зависшие задачи; удаляет старые обезличенные tombstone-записи."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE expires <= ? OR (status IN ('queued','processing') AND updated <= ?)",
                (now, stale_before),
            ).fetchall()
            db.execute("DELETE FROM jobs WHERE status='expired' AND updated < ?", (now - 3600,))
        values = []
        for row in rows:
            data = dict(row)
            data["ai_progress"] = json.loads(data["ai_progress"])
            values.append(ComparisonJob(**data))
        return values

    def requeue_ai(self, job_id: str, expires: float, max_session_jobs: int = 2) -> bool:
        """Атомарно повторяет только завершённый ИИ-проход с сохранённым базовым результатом."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT owner FROM jobs WHERE id=?", (job_id,)).fetchone()
            if owner is None:
                return False
            active = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? AND status IN ('queued','processing')",
                (owner[0],),
            ).fetchone()[0]
            if active >= max_session_jobs:
                raise DocumentError("Превышен лимит активных заданий сессии")
            cursor = db.execute(
                "UPDATE jobs SET status='queued', stage='Повтор ИИ-проверки', resume_ai=1, lease='', error='', expires=?, updated=? WHERE id=? AND status IN ('completed','completed_with_warnings','failed','cancelled') AND use_ai=1",
                (expires, time.time(), job_id),
            )
            return cursor.rowcount == 1
