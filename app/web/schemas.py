# app/web/schemas.py
"""Типизированные публичные DTO; владельцы, пути и capability в ответы не входят."""

from datetime import datetime

from pydantic import BaseModel, Field


class JobResponse(BaseModel):
    """Реальное состояние задания и фактическое время истечения хранения."""

    id: str
    status: str
    stage: str
    created: float
    updated: float
    expires: float
    use_ai: bool
    old_name: str
    new_name: str
    error: str
    ai_full: bool = False
    ai_unverified: bool = False
    ai_progress: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Единая безопасная ошибка API для клиента и OpenAPI."""

    detail: str
    code: str | None = None


class IntegrationJobResponse(BaseModel):
    """Технический контракт бота без содержимого документов и внутренних идентификаторов доступа."""

    job_id: str
    status: str
    stage: str
    ai_full: bool
    ai_unverified: bool
    result_ready: bool
    ai: dict = Field(default_factory=dict)
    statistics: dict[str, int] | None = None
    expires_at: datetime | None = None
    error_code: str | None = None


class AccessLinkResponse(BaseModel):
    """Одноразовая переносимая ссылка, ограниченная сроком хранения результата."""

    job_id: str
    url: str
    link_expires_at: datetime
    result_expires_at: datetime


class AccessExchange(BaseModel):
    """Билет передаётся только в теле POST и не используется в URL сервера."""

    ticket: str = Field(min_length=40, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
