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
                ("integration_client", "TEXT NOT NULL DEFAULT ''"),
                ("idempotency_key", "TEXT NOT NULL DEFAULT ''"),
                ("request_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
                    if name == "ai_full":
                        connection.execute("UPDATE jobs SET ai_full=use_ai")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_integration_key ON jobs(integration_client,idempotency_key) WHERE idempotency_key != ''"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS result_access (digest TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, kind TEXT NOT NULL, expires REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS request_limits (scope TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL)"
            )

    @contextmanager
    def connection(self):
        """Ограничивает ожидание блокировки и гарантирует закрытие соединения."""
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(
        self, job: ComparisonJob, max_jobs: int, max_session_jobs: int
    ) -> ComparisonJob | None:
        """Атомарно проверяет идемпотентность и квоты; только первый запрос резервирует задание."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if job.idempotency_key:
                previous = db.execute(
                    "SELECT * FROM jobs WHERE integration_client=? AND idempotency_key=?",
                    (job.integration_client, job.idempotency_key),
                ).fetchone()
                if previous:
                    if previous["request_fingerprint"] != job.request_fingerprint:
                        raise DocumentError(
                            "Ключ уже использован с другими файлами или параметрами",
                            status_code=409,
                            code="idempotency_conflict",
                        )
                    return self._job(previous)
            total = db.execute("SELECT COUNT(*) FROM jobs WHERE status != 'expired'").fetchone()[0]
            owned = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? AND status IN ('queued','processing')",
                (job.owner,),
            ).fetchone()[0]
            if total >= max_jobs or owned >= max_session_jobs:
                raise DocumentError(
                    "Достигнут лимит заданий; дождитесь завершения или удаления предыдущих",
                    status_code=429,
                    code="job_limit",
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
        return self._job(row) if row else None

    @staticmethod
    def _job(row) -> ComparisonJob:
        """Восстанавливает одну техническую запись из единого формата SQLite."""
        values = dict(row)
        values["ai_progress"] = json.loads(values["ai_progress"])
        return ComparisonJob(**values)

    def rate_limit(self, scope: str, maximum: int, now: float) -> bool:
        """Резервирует запрос до чтения тела; счётчик общий для процессов API."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM request_limits WHERE started <= ?", (now - 60,))
            db.execute(
                "INSERT INTO request_limits VALUES (?, ?, 0) ON CONFLICT DO NOTHING", (scope, now)
            )
            return (
                db.execute(
                    "UPDATE request_limits SET count=count+1 WHERE scope=? AND count < ?",
                    (scope, maximum),
                ).rowcount
                == 1
            )

    def create_access(
        self, job_id: str, owner: str, digest: str, expires: float, now: float
    ) -> float:
        """Выдаёт билет только завершённому результату клиента; в БД попадает лишь SHA256."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND integration_client=?", (job_id, owner)
            ).fetchone()
            if not row:
                raise DocumentError("Задание не найдено", status_code=404, code="not_found")
            if row["expires"] <= now or row["status"] == "expired":
                raise DocumentError("Срок хранения истёк", status_code=410, code="result_expired")
            if row["status"] not in {"completed", "completed_with_warnings"}:
                raise DocumentError("Результат ещё не готов", status_code=409, code="not_ready")
            expires = min(expires, row["expires"])
            db.execute("DELETE FROM result_access WHERE job_id=? AND kind='ticket'", (job_id,))
            db.execute(
                "INSERT INTO result_access VALUES (?, ?, 'ticket', ?)", (digest, job_id, expires)
            )
            return expires

    def activate_access(self, digest: str, session_digest: str, now: float) -> tuple[str, float]:
        """Одной транзакцией расходует билет и создаёт сессию чтения ровно одного результата."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT a.job_id,j.expires FROM result_access a JOIN jobs j ON j.id=a.job_id WHERE a.digest=? AND a.kind='ticket' AND a.expires>? AND j.expires>? AND j.status IN ('completed','completed_with_warnings')",
                (digest, now, now),
            ).fetchone()
            if not row:
                raise DocumentError(
                    "Ссылка истекла или уже использована", status_code=410, code="link_unavailable"
                )
            db.execute("DELETE FROM result_access WHERE digest=?", (digest,))
            db.execute(
                "INSERT INTO result_access VALUES (?, ?, 'session', ?)",
                (session_digest, row["job_id"], row["expires"]),
            )
            return row["job_id"], row["expires"]

    def can_view(self, job_id: str, digest: str, now: float) -> bool:
        """Браузерный токен разрешает только чтение своего ещё доступного задания."""
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM result_access a JOIN jobs j ON j.id=a.job_id WHERE a.digest=? AND a.job_id=? AND a.kind='session' AND a.expires>? AND j.expires>? AND j.status!='expired'",
                    (digest, job_id, now, now),
                ).fetchone()
                is not None
            )

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
                "SELECT * FROM jobs WHERE status!='expired' AND (expires <= ? OR (status IN ('queued','processing') AND updated <= ?))",
                (now, stale_before),
            ).fetchall()
            db.execute("DELETE FROM jobs WHERE status='expired' AND updated < ?", (now - 3600,))
            db.execute("DELETE FROM result_access WHERE expires<=?", (now,))
            db.execute("DELETE FROM request_limits WHERE started<=?", (now - 60,))
        return [self._job(row) for row in rows]

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
