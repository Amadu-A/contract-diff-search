# app/web/schemas.py
"""Типизированные публичные DTO; владельцы, пути и capability в ответы не входят."""

from pydantic import BaseModel


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


class ErrorResponse(BaseModel):
    """Единая безопасная ошибка API для клиента и OpenAPI."""

    detail: str
