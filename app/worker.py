# app/worker.py
"""Точка входа ограниченного Celery worker из той же кодовой базы монолита."""

from app.bootstrap import build_service
from app.infrastructure.queue import make_celery
from app.observability import configure_logging
from app.settings import Settings

settings = Settings()
configure_logging(settings.log_level)
celery_app = make_celery(settings)


@celery_app.task(name="contract_diff.process")
def process(job_id: str) -> None:
    """Передаёт доставленный UUID единому use-case с атомарным захватом задания."""
    build_service(settings).process(job_id)


def main() -> None:
    """Ограничивает число процессов значением Pydantic settings и отключает лишний обмен."""
    celery_app.worker_main(
        [
            "worker",
            "--loglevel=WARNING",
            f"--concurrency={settings.worker_concurrency}",
            "--without-gossip",
            "--without-mingle",
        ]
    )


if __name__ == "__main__":
    main()
