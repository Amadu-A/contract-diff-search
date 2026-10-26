# app/application/queries.py
"""Чистые прикладные проекции результата для интерфейса, фильтров и навигации."""

from collections import Counter

from app.domain.comparison import statistics
from app.domain.models import Difference


def summary(data: dict) -> dict:
    """Убирает файловые пути и тяжёлые слова; статистика следует единому доменному правилу."""
    documents = {
        side: {k: data[side][k] for k in ("name", "format", "pages", "signed", "visual_only")}
        for side in ("old", "new")
    }
    exports = {
        side: [
            {"format": e["format"], "available": bool(e["path"]), "reason": e["reason"]}
            for e in items
        ]
        for side, items in data["exports"].items()
    }
    return {
        "statistics": statistics([Difference(**d) for d in data["differences"]]),
        "documents": documents,
        "warnings": data["warnings"],
        "ai_status": data["ai_status"],
        "exports": exports,
        "coverage": data["coverage"],
    }


def filter_differences(data: dict, category: str, status: str, offset: int, limit: int) -> dict:
    """Фильтрует уникальные изменения, не меняя исходный результат и покрытие."""
    items = [
        d
        for d in data["differences"]
        if (not category or d["category"] == category)
        and (
            not status
            or d["status"] == status
            or status == "unverified"
            and d["category"] == "unverified_region"
        )
    ]
    return {"total": len(items), "items": items[offset : offset + limit]}


def search_text(data: dict, side: str, query: str) -> dict:
    """Находит извлечённый/OCR-текст и возвращает только подтверждённую геометрию слов."""
    found = []
    for element in data[side]["elements"]:
        start = element["text"].casefold().find(query.casefold())
        if start >= 0:
            areas = [
                w["region"]
                for w in element["words"]
                if w["end"] > start and w["start"] < start + len(query)
            ]
            found.append(
                {"element": element["id"], "text": element["text"][:300], "regions": areas}
            )
            if len(found) >= 200:
                break
    return {"items": found}


def page_regions(data: dict, side: str, page: int) -> dict:
    """Сохраняет все маркеры страницы независимо от текущих фильтров списка."""
    return {
        "items": [
            {"id": d["id"], "category": d["category"], "status": d["status"], "region": r}
            for d in data["differences"]
            for r in d[f"{side}_regions"]
            if r["page"] == page
        ]
    }


def correspondence(data: dict, side: str, page: int) -> dict:
    """Выбирает связанную страницу по голосам сопоставленных элементов; ничью не угадывает."""
    other = "new" if side == "old" else "old"
    own = {e["id"]: e for e in data[side]["elements"]}
    partner = {e["id"]: e for e in data[other]["elements"]}
    votes = Counter()
    for match in data["matches"]:
        if not any(
            w["region"]["page"] == page
            for identifier in match[side]
            for w in own[identifier]["words"]
        ):
            continue
        for identifier in match[other]:
            for word in partner[identifier]["words"]:
                votes[word["region"]["page"]] += 1
    ranked = votes.most_common(2)
    selected = (
        ranked[0][0] if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]) else None
    )
    return {"page": selected, "method": "matched_elements" if selected else "ambiguous"}
