# app/application/queries.py
"""Чистые прикладные проекции результата для интерфейса, фильтров и навигации."""

from collections import Counter

from app.domain.comparison import statistics
from app.domain.models import Difference
from app.domain.visibility import visible


def summary(data: dict) -> dict:
    """Убирает файловые пути и тяжёлые слова; статистика следует единому доменному правилу."""
    documents = {
        side: {k: data[side][k] for k in ("name", "format", "pages", "signed", "visual_only")}
        for side in ("old", "new")
    }
    exports = {
        side: [
            {
                "format": e["format"],
                "available": bool(e["path"]) and data.get("exports_current", True),
                "reason": e["reason"]
                if data.get("exports_current", True)
                else "Экспорт с текущими результатами ИИ ещё готовится",
                "show_unverified": e.get("show_unverified", True),
            }
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
        "ai_progress": {
            name: data.get("ai_review", {}).get(name, {}) for name in ("full", "unverified")
        },
        "verification_metrics": {
            "original_unverified": data["coverage"].get("original_unverified", 0),
            "resolved_algorithms": data["coverage"].get("resolved_algorithms", 0),
            "resolved_ai": data["coverage"].get("resolved_ai", 0),
            "new_unverified": data["coverage"].get("new_unverified", 0),
            "remaining_unverified": statistics([Difference(**d) for d in data["differences"]])[
                "unverified"
            ],
        },
    }


def filter_differences(
    data: dict, category: str, status: str, offset: int, limit: int, show_unverified: bool = True
) -> dict:
    """Фильтрует уникальные изменения, не меняя исходный результат и покрытие."""
    items = [
        d
        for d in data["differences"]
        if visible(d, show_unverified)
        and (not category or d["category"] == category)
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


def page_regions(data: dict, side: str, page: int, show_unverified: bool = True) -> dict:
    """Сохраняет все маркеры страницы независимо от текущих фильтров списка."""
    return {
        "items": [
            {
                "id": d["id"],
                "category": d["category"],
                "status": d["status"],
                "source": d["source"],
                "region": r,
            }
            for d in data["differences"]
            if visible(d, show_unverified)
            for r in d[f"{side}_regions"]
            if r["page"] == page
        ]
    }


def ai_operations(data: dict, offset: int, limit: int) -> dict:
    """Отдаёт технический журнал единиц владельцу без private-path, текста и изображений."""
    units = data.get("ai_review", {}).get("units", [])
    return {
        "total": len(units),
        "items": [
            {
                key: unit.get(key)
                for key in (
                    "id",
                    "mode",
                    "kind",
                    "status",
                    "outcome",
                    "attempts",
                    "error_kind",
                    "duration_ms",
                )
            }
            for unit in units[offset : offset + limit]
        ],
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
