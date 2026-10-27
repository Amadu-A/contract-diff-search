# app/infrastructure/queue.py
"""Celery поверх общего RabbitMQ: отдельная очередь, ограниченная доставка."""

from celery import Celery
from kombu import Queue

from app.domain.models import DocumentError
from app.settings import Settings


def make_celery(settings: Settings) -> Celery:
    """Создаёт клиент без подключения; broker URL никогда не выводится приложением."""
    app = Celery("contract_diff", broker=settings.broker_url)
    app.conf.update(
        task_queues=[Queue(settings.queue_name, durable=True)],
        task_default_queue=settings.queue_name,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_ignore_result=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        worker_hijack_root_logger=False,
        task_reject_on_worker_lost=True,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=5,
        task_publish_retry=False,
        task_soft_time_limit=settings.job_timeout_seconds,
        task_time_limit=settings.job_timeout_seconds + 15,
        worker_max_tasks_per_child=10,
        worker_max_memory_per_child=1500000,
        broker_transport_options={"confirm_publish": True},
    )
    return app


class CeleryPublisher:
    """Публикует только UUID и использует отдельный application vhost."""

    def __init__(self, client: Celery):
        """Принимает сконфигурированный клиент без создания брокера."""
        self.client = client

    def publish(self, job_id: str) -> None:
        """Ограничивает ожидание недоступного RabbitMQ и сообщает понятную ошибку."""
        try:
            self.client.send_task(
                "contract_diff.process", args=[job_id], task_id=job_id, retry=False, expires=3600
            )
        except Exception as exc:
            raise DocumentError("Очередь обработки недоступна; проверьте общий RabbitMQ") from exc
