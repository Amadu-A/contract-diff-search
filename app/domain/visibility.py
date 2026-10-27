# app/domain/visibility.py
"""Единая политика видимости предупреждений для выдачи и всех экспортных форматов."""

from app.domain.models import Difference


def is_unverified(item: Difference | dict) -> bool:
    """Относит к предупреждениям только активные непроверенные/неоднозначные события."""
    values = item if isinstance(item, dict) else vars(item)
    return values.get("active", True) and (
        values.get("status") in {"unverified", "ambiguous"}
        or values.get("category") in {"unverified_region", "ambiguous_match"}
    )


def visible(item: Difference | dict, show_unverified: bool = True) -> bool:
    """Скрытие не меняет статус, статистику или исходное содержание документа."""
    values = item if isinstance(item, dict) else vars(item)
    return values.get("active", True) and (show_unverified or not is_unverified(item))


def uncertainty_ids(differences: list[Difference]) -> set[str]:
    """Считает одну причину покрытия один раз при нескольких локальных проявлениях."""
    return {d.uncertainty_id or d.id for d in differences if is_unverified(d)}
