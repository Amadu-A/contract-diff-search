# app/application/queries.py
"""Чистые прикладные проекции результата для интерфейса, фильтров и навигации."""

from collections import Counter
from datetime import UTC, datetime

from app.domain.comparison import statistics
from app.domain.models import TERMINAL, Difference, JobStatus
from app.domain.text import comparison_tokens
from app.domain.visibility import is_unverified, visible


def comparison_report(
    data: dict,
    job,
    show_unverified: bool = True,
    category: str | list[str] | tuple[str, ...] | None = (),
    status: str = "",
) -> dict:
    """Готовит факты отчёта с учётом видимости, без оформления и структуры таблиц; статусы не меняет."""
    unique = {}
    for item in filter_differences(
        data, category, status, 0, len(data["differences"]), show_unverified
    )["items"]:
        if not visible(item, show_unverified) or item["category"] in {
            "formatting_changed",
            "table_structure_changed",
        }:
            continue
        key = tuple(
            str(item.get(name, ""))
            for name in (
                "category",
                "old_section",
                "new_section",
                "old_text",
                "new_text",
                "old_element",
                "new_element",
                "old_range",
                "new_range",
                "status",
                "old_regions",
                "new_regions",
            )
        )
        if key not in unique:
            unique[key] = dict(item)
        elif unique[key].get("source") != item.get("source"):
            unique[key]["verified_by"] = "algorithms_and_ai"
    items = list(unique.values())
    return {
        "old_name": data["old"]["name"],
        "new_name": data["new"]["name"],
        "ai_full": bool(job.ai_full),
        "ai_unverified": bool(job.ai_unverified),
        "ai_status": data.get("ai_status", "not_requested"),
        "statistics": statistics([Difference(**item) for item in items]),
        "differences": items,
    }


def integration_status(job, data: dict | None) -> dict:
    """Проецирует текущие счётчики для внешнего клиента без содержимого документов и предварительного TTL."""
    ready = job.status in {JobStatus.COMPLETED, JobStatus.WARNINGS} and data is not None
    return {
        "job_id": job.id,
        "status": job.status,
        "stage": "completed" if ready else job.stage,
        "ai_full": bool(job.ai_full),
        "ai_unverified": bool(job.ai_unverified),
        "result_ready": ready,
        "ai": {
            mode: job.ai_progress.get(
                mode, {"status": "queued" if getattr(job, "ai_" + mode) else "not_requested"}
            )
            for mode in ("full", "unverified")
        },
        "statistics": statistics([Difference(**d) for d in data["differences"]]) if data else None,
        "expires_at": datetime.fromtimestamp(job.expires, UTC) if job.status in TERMINAL else None,
        "error_code": "processing_failed"
        if job.status == JobStatus.FAILED
        else "result_expired"
        if job.status == JobStatus.EXPIRED
        else None,
    }


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
                "available": bool(e["path"])
                and data.get("exports_current", True)
                and (e["format"] == "pdf" or bool(e.get("confirmed_only"))),
                "reason": (
                    e["reason"]
                    if e["format"] == "pdf" or e.get("confirmed_only")
                    else "Для нового DOCX выполните сравнение заново"
                )
                if data.get("exports_current", True)
                else "Экспорт с текущими результатами ИИ ещё готовится",
                "show_unverified": e.get("show_unverified", True),
                "confirmed_only": e.get("confirmed_only", False),
                "marked_changes": e.get("marked_changes", 0),
                "unmarked_changes": e.get("unmarked_changes", 0),
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
    data: dict,
    category: str | list[str] | tuple[str, ...] | None,
    status: str,
    offset: int,
    limit: int,
    show_unverified: bool = True,
) -> dict:
    """Фильтрует уникальные изменения, не меняя исходный результат и покрытие."""
    categories = {category} if isinstance(category, str) and category else set(category or ())
    categories.discard("")
    items = [
        d
        for d in data["differences"]
        if visible(d, show_unverified)
        and (
            not categories
            or d["category"] in categories
            or "unverified_region" in categories
            and is_unverified(d)
        )
        and (
            not status
            or (
                is_unverified(d)
                if status == "unverified"
                else d["status"] == status and not (status == "confirmed" and is_unverified(d))
            )
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
                    "verified_by",
                )
            }
            for unit in units[offset : offset + limit]
        ],
    }


def _nearby_word_page(element, partners, span, exclusions):
    """Ищет уникальные соседние тройки слов внутри уже сопоставленного блока."""
    if not span:
        return None
    exclusions = (
        (tuple(exclusions),)
        if exclusions and isinstance(exclusions[0], str)
        else tuple(tuple(group) for group in exclusions)
    )

    def located_tokens(elements):
        """Связывает поисковые токены с настоящими проверенными страницами слов."""
        result = []
        for item in elements:
            for text, start, end in comparison_tokens(item["text"], exclusions):
                pages = {
                    w["region"]["page"]
                    for w in item["words"]
                    if w["end"] > start
                    and w["start"] < end
                    and w.get("verified", True)
                    and w.get("confidence", 1) >= 0.65
                }
                result.append((text, start, end, next(iter(pages)) if len(pages) == 1 else None))
        return result

    source = located_tokens([element])
    target = located_tokens(partners)
    source_values = [t[0] for t in source]
    target_values = [t[0] for t in target]
    before = sum(t[2] <= span[0] for t in source)
    after = next((i for i, t in enumerate(source) if t[1] >= span[1]), len(source))
    anchors = []
    for boundary, direction in ((before, -1), (after, 1)):
        for distance in range(8):
            start = boundary - 3 - distance if direction < 0 else boundary + distance
            if start < 0 or start + 3 > len(source):
                break
            needle = source_values[start : start + 3]
            if sum(any(c.isalnum() for c in token) for token in needle) < 2:
                continue
            own = [i for i in range(len(source) - 2) if source_values[i : i + 3] == needle]
            hits = [i for i in range(len(target) - 2) if target_values[i : i + 3] == needle]
            if (
                len(own) != 1
                or len(hits) != 1
                or any(t[3] is None for t in source[start : start + 3])
            ):
                continue
            pages = {t[3] for t in target[hits[0] : hits[0] + 3]}
            if len(pages) == 1 and None not in pages:
                anchors.append(next(iter(pages)))
                break
    return anchors[0] if anchors and len(set(anchors)) == 1 else None


def correspondence(data: dict, side: str, page: int, difference_id: str = "") -> dict:
    """Выбирает связанную страницу по голосам сопоставленных элементов; ничью не угадывает."""
    other = "new" if side == "old" else "old"
    own = {e["id"]: e for e in data[side]["elements"]}
    partner = {e["id"]: e for e in data[other]["elements"]}

    def reliable(match):
        """Различает качество текстового сходства и однозначную структурную привязку пункта."""
        if match.get("confidence", 0) >= 0.65:
            return True
        if len(match[side]) == len(match[other]) == 1:
            left, right = own.get(match[side][0]), partner.get(match[other][0])
            return bool(left and right and left["section"] and left["section"] == right["section"])
        return False

    if difference_id:
        difference = next((d for d in data["differences"] if d["id"] == difference_id), None)
        identifier = difference.get(side + "_element") if difference else None
        if identifier not in own:
            if difference and any(r["page"] == page for r in difference[side + "_regions"]):
                return correspondence(data, side, page)
            return {"page": None, "method": "missing_anchor"}
        positions = {key: i for i, key in enumerate(own)}
        origin = positions[identifier]
        for match in data["matches"]:
            if identifier not in match[side]:
                continue
            partners = [partner[key] for key in match[other] if key in partner]
            selected = _nearby_word_page(
                own[identifier],
                partners,
                difference.get(side + "_range"),
                data.get("comparison_exclusions", []),
            )
            if selected:
                return {"page": selected, "method": "matched_words"}
            pages = {w["region"]["page"] for e in partners for w in e["words"]}
            if reliable(match) and len(pages) == 1:
                return {"page": next(iter(pages)), "method": "matched_element"}
        neighbors = []
        for match in data["matches"]:
            if match.get("source") == "ai" or not reliable(match):
                continue
            indices = [positions[key] for key in match[side] if key in positions]
            if not indices or min(indices) <= origin <= max(indices):
                continue
            before = max(indices) < origin
            target_pages = [
                w["region"]["page"] for key in match[other] for w in partner[key]["words"]
            ]
            if not target_pages:
                continue
            index = max(indices) if before else min(indices)
            neighbors.append(
                (
                    before,
                    abs(origin - index),
                    max(target_pages) if before else min(target_pages),
                    own[match[side][0]]["section"] == own[identifier]["section"],
                    any(
                        w["region"]["page"] == page
                        for key in match[side]
                        for w in own[key]["words"]
                    ),
                )
            )
        nearest = [
            min((n for n in neighbors if n[0] == before), key=lambda n: n[1], default=None)
            for before in (True, False)
        ]
        anchors = [n for n in nearest if n]
        if len(anchors) == 2 and anchors[0][2] == anchors[1][2]:
            return {"page": anchors[0][2], "method": "neighbor_anchors"}
        if len(anchors) == 1 and anchors[0][3] and anchors[0][4]:
            return {"page": anchors[0][2], "method": "section_anchor"}
        return {"page": None, "method": "ambiguous"}
    votes = Counter()
    for match in data["matches"]:
        if match.get("source") == "ai":
            if any(r["page"] == page for r in match.get(side + "_regions", [])):
                votes.update(r["page"] for r in match.get(other + "_regions", []))
            continue
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
