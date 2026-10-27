# app/application/ai_evidence.py
"""Независимая привязка цитат ИИ к проверенным словам и локальное разрешение неопределённости."""

import hashlib
from dataclasses import dataclass

from app.domain.comparison import regions
from app.domain.models import ComparisonResult, Difference, ReviewUnit, VisualRegion
from app.domain.text import normalize
from app.domain.visibility import is_unverified, uncertainty_ids


@dataclass
class Anchor:
    """Найденная в единственном исходном диапазоне цитата с измеренной геометрией."""

    element: str | None
    span: tuple[int, int] | None
    areas: list[VisualRegion]
    verified: bool


def _overlap(a: VisualRegion, b: VisualRegion) -> bool:
    """Проверяет пересечение исходных областей в одной видимой системе координат."""
    return a.page == b.page and a.x1 > b.x0 and b.x1 > a.x0 and a.y1 > b.y0 and b.y1 > a.y0


def _span(text: str, value: str, quote: str) -> tuple[int, int] | None:
    """Требует единственности контекстной цитаты и значения внутри неё, сохраняя числа и пунктуацию."""
    normalized, needle = normalize(text), normalize(quote or value).text
    if not needle or normalized.text.count(needle) != 1:
        return None
    begin = normalized.text.index(needle)
    if value:
        literal = normalize(value).text
        if needle.count(literal) != 1:
            return None
        begin += needle.index(literal)
        end = begin + len(literal)
    else:
        end = begin + len(needle)
    return normalized.positions[begin], normalized.positions[end - 1] + 1


def anchor(
    result: ComparisonResult, unit: ReviewUnit, side: str, value: str, quote: str, reference: str
) -> Anchor | None:
    """Принимает только предложенный ref и цитату, найденную в соответствующей локальной области."""
    areas = getattr(unit, side + "_regions")
    prefix = "o" if side == "old" else "n"
    if not reference.startswith(prefix) or not reference[1:].isdigit():
        return None
    index = int(reference[1:])
    if index >= len(areas):
        return None
    selected = areas[index]
    version = getattr(result, side)
    found = []
    for element in version.elements:
        if element.id not in getattr(unit, side + "_elements"):
            continue
        span = _span(element.text, value, quote)
        if not span:
            continue
        words = [w for w in element.words if w.end > span[0] and w.start < span[1]]
        local = [w for w in words if _overlap(w.region, selected)]
        covered = all(
            element.text[index].isspace() or any(w.start <= index < w.end for w in words)
            for index in range(*span)
        )
        if words and covered and len(local) == len(words):
            found.append(
                Anchor(
                    element.id,
                    span,
                    regions(element, *span),
                    all(w.verified and w.confidence >= 0.65 for w in words),
                )
            )
    if len(found) == 1 and found[0].verified:
        return found[0]
    # Независимое перечитывание исходного растра не заменяет текст другой редакции.
    evidence = [w for w in getattr(unit, side + "_evidence") if _overlap(w.region, selected)]
    text = " ".join(w.text for w in evidence)
    span = _span(text, value, quote)
    if span:
        offset, selected_words = 0, []
        for word in evidence:
            end = offset + len(word.text)
            if end > span[0] and offset < span[1]:
                selected_words.append(word)
            offset = end + 1
        if selected_words:
            identifiers = getattr(unit, side + "_elements")
            return Anchor(
                identifiers[0] if len(identifiers) == 1 else None,
                None,
                [w.region for w in selected_words],
                all(w.verified and w.confidence >= 0.65 for w in selected_words),
            )
    return found[0] if len(found) == 1 else None


def _duplicate(existing: Difference, new: Difference) -> bool:
    """Не считает дважды тот же факт страницы/блока даже при расширенной цитате модели."""
    if not existing.active:
        return False
    if existing.category != new.category:
        family = {"text_modified", "number_modified", "table_modified"}
        if not (
            {existing.category, new.category} <= family
            and normalize(existing.old_text).text == normalize(new.old_text).text
            and normalize(existing.new_text).text == normalize(new.new_text).text
        ):
            return False
    same = existing.old_element == new.old_element and existing.new_element == new.new_element
    texts = all(
        not a and not b or bool(a and b and (a in b or b in a))
        for a, b in ((existing.old_text, new.old_text), (existing.new_text, new.new_text))
    )
    geometry = any(_overlap(a, b) for a in existing.old_regions for b in new.old_regions) or any(
        _overlap(a, b) for a in existing.new_regions for b in new.new_regions
    )
    return texts and (same or geometry)


def _corresponds(result, old_element, new_element):
    """Требует независимого структурного соответствия и не смешивает разные колонки сторон договора."""
    if not old_element or not new_element:
        return False
    a = next(e for e in result.old.elements if e.id == old_element)
    b = next(e for e in result.new.elements if e.id == new_element)
    left, right = a.attributes.get("column"), b.attributes.get("column")
    if left is not None and right is not None and left != right:
        return False
    return any(
        old_element in match.old and new_element in match.new and match.confidence >= 0.7
        for match in result.matches
    )


def merge_reply(result: ComparisonResult, unit: ReviewUnit, reply) -> None:
    """Сохраняет подтверждённые алгоритмы и добавляет только локализованные независимые замечания."""
    unit.old_evidence, unit.new_evidence = reply.old_evidence, reply.new_evidence
    if (
        reply.outcome == "same"
        and reply.complete
        and reply.readable
        and unit.kind not in {"correspondence", "coverage"}
    ):
        _record_contradiction(result, unit)
    _resolve_refined_same(result, unit, reply)
    resolved = set()
    for observation in [*reply.findings, *reply.evidence]:
        a = anchor(
            result, unit, "old", observation.old_value, observation.old_quote, observation.old_ref
        )
        b = anchor(
            result, unit, "new", observation.new_value, observation.new_quote, observation.new_ref
        )
        same = observation.category == "unchanged"
        confirmed = bool(
            reply.readable
            and a
            and b
            and a.verified
            and b.verified
            and _corresponds(result, a.element, b.element)
        )
        if observation.category in {"unverified_region", "text_added", "text_deleted"}:
            confirmed = False
        if same:
            confirmed = (
                confirmed
                and normalize(observation.old_value).text == normalize(observation.new_value).text
            )
        else:
            confirmed = (
                confirmed
                and normalize(observation.old_value).text != normalize(observation.new_value).text
            )
        if same and confirmed:
            for difference in result.differences:
                root = difference.uncertainty_id or difference.id
                if not is_unverified(difference) or (unit.targets and root not in unit.targets):
                    continue
                # Общее предупреждение абзаца снимается только при проверке всего его содержания.
                if difference.category == "unverified_region":
                    old_text, new_text = (
                        normalize(difference.old_text).text,
                        normalize(difference.new_text).text,
                    )
                    covers = (
                        not old_text or old_text == normalize(observation.old_value).text
                    ) and (not new_text or new_text == normalize(observation.new_value).text)
                else:
                    covers = (
                        difference.old_text == observation.old_value
                        and difference.new_text == observation.new_value
                    )
                localized = (
                    not difference.old_element or difference.old_element == a.element
                ) and (not difference.new_element or difference.new_element == b.element)
                if covers and localized and (difference.old_text or difference.new_text):
                    difference.active = False
                    resolved.add(root)
                    result.ai_review.resolutions[root] = "same"
                    for side, located in (("old", a), ("new", b)):
                        if located.element:
                            getattr(result.coverage, side)[located.element] = "unchanged"
            continue
        if same:
            continue
        ar = a.areas if a else _reference_regions(unit.old_regions, observation.old_ref, "o")
        br = b.areas if b else _reference_regions(unit.new_regions, observation.new_ref, "n")
        fingerprint = (
            unit.mode,
            observation.category,
            observation.old_value,
            observation.new_value,
            [(r.page, r.x0, r.y0, r.x1, r.y1) for r in ar + br],
        )
        identifier = "AI" + hashlib.sha256(repr(fingerprint).encode()).hexdigest()[:16]
        candidate = Difference(
            identifier,
            observation.category,
            "ИИ: найдено различие" if confirmed else "ИИ: требуется проверка",
            observation.old_value,
            observation.new_value,
            old_regions=ar,
            new_regions=br,
            old_element=a.element if a else None,
            new_element=b.element if b else None,
            old_range=a.span if a else None,
            new_range=b.span if b else None,
            status="confirmed" if confirmed else "needs_review",
            source="ai",
            reason=observation.reason,
            ai_mode=unit.mode,
            verified_by="ai_and_independent_text" if confirmed else "",
            confidence=observation.confidence,
            uncertainty_id=unit.targets[0]
            if observation.category == "unverified_region" and len(unit.targets) == 1
            else "",
        )
        for side, located in (("old", a), ("new", b)):
            if located and located.element:
                element = next(e for e in getattr(result, side).elements if e.id == located.element)
                setattr(candidate, side + "_section", element.section)
        duplicates = [d for d in result.differences if _duplicate(d, candidate)]
        if duplicates:
            for difference in duplicates:
                if confirmed and (
                    is_unverified(difference)
                    or difference.source == "ai"
                    and difference.status == "needs_review"
                ):
                    difference.status = "confirmed"
                    difference.verified_by = "ai_and_independent_text"
                    difference.ai_mode = unit.mode
                    difference.old_regions, difference.new_regions = a.areas, b.areas
                    difference.old_range, difference.new_range = a.span, b.span
                    resolved.add(difference.uncertainty_id or difference.id)
                    root = difference.uncertainty_id or difference.id
                    if root in result.ai_review.original_uncertainties:
                        result.ai_review.resolutions[root] = "different"
            continue
        if not any(d.id == identifier for d in result.differences):
            result.differences.append(candidate)
        if confirmed:
            for difference in result.differences:
                root = difference.uncertainty_id or difference.id
                if (
                    root in unit.targets
                    and difference.category == "unverified_region"
                    and is_unverified(difference)
                    and (
                        not difference.old_text
                        or normalize(difference.old_text).text
                        == normalize(observation.old_value).text
                    )
                    and (
                        not difference.new_text
                        or normalize(difference.new_text).text
                        == normalize(observation.new_value).text
                    )
                ):
                    difference.active = False
                    resolved.add(root)
                    result.ai_review.resolutions[root] = "different"
            for side, located in (("old", a), ("new", b)):
                if located.element:
                    getattr(result.coverage, side)[located.element] = "changed"
    remaining = uncertainty_ids(result.differences)
    result.coverage.resolved_ai = len(set(result.ai_review.original_uncertainties) - remaining)
    result.coverage.new_unverified = len(remaining - set(result.ai_review.original_uncertainties))
    result.ai_review.resolutions = {
        key: value for key, value in result.ai_review.resolutions.items() if key not in remaining
    }


def _record_contradiction(result, unit):
    """Оставляет проверенные алгоритмами факты и отдельно показывает противоречие с ответом модели."""
    for difference in list(result.differences):
        if (
            not difference.active
            or difference.source != "deterministic"
            or difference.status != "confirmed"
        ):
            continue
        intersects = bool(
            difference.old_text
            and difference.new_text
            and difference.old_regions
            and difference.new_regions
            and difference.old_element in unit.old_elements
            and difference.new_element in unit.new_elements
        )
        for side in ("old", "new"):
            intersects = intersects and all(
                any(
                    r.page == area.page
                    and area.x0 <= r.x0
                    and area.y0 <= r.y0
                    and area.x1 >= r.x1
                    and area.y1 >= r.y1
                    for area in getattr(unit, side + "_regions")
                )
                for r in getattr(difference, side + "_regions")
            )
        if not intersects or difference.category in {"unverified_region", "ambiguous_match"}:
            continue
        identifier = "AI-C-" + difference.id
        if any(d.id == identifier for d in result.differences):
            continue
        result.differences.append(
            Difference(
                identifier,
                "ai_contradiction",
                "ИИ: противоречие с подтверждённым различием",
                difference.old_text,
                difference.new_text,
                old_regions=difference.old_regions,
                new_regions=difference.new_regions,
                old_element=difference.old_element,
                new_element=difference.new_element,
                status="needs_review",
                source="ai",
                reason="Модель сообщила о совпадении; независимое сравнение обнаружило различие. Подтверждённый факт сохранён.",
                ai_mode=unit.mode,
            )
        )


def _resolve_refined_same(result, unit, reply):
    """Снимает ложные OCR-расхождения только при полном совпадении двух независимо перечитанных областей."""
    if not reply.complete or not reply.readable or reply.outcome != "same" or not unit.targets:
        return
    if len(unit.old_elements) != 1 or len(unit.new_elements) != 1:
        return
    a = next(e for e in result.old.elements if e.id == unit.old_elements[0])
    b = next(e for e in result.new.elements if e.id == unit.new_elements[0])
    if not _corresponds(result, a.id, b.id):
        return
    if unit.old_text != a.text or unit.new_text != b.text:
        return
    words = [*reply.old_evidence, *reply.new_evidence]
    if (
        not reply.old_evidence
        or not reply.new_evidence
        or not all(w.verified and w.confidence >= 0.65 for w in words)
    ):
        return
    left = normalize(" ".join(w.text for w in reply.old_evidence)).text
    right = normalize(" ".join(w.text for w in reply.new_evidence)).text
    if not left or left != right:
        return
    for side, element in (("old", a), ("new", b)):
        if not element.words or float(element.attributes.get("geometry_coverage", "1")) < 0.98:
            return
        if any(
            not any(_overlap(w.region, r) for r in getattr(unit, side + "_regions"))
            for w in element.words
        ):
            return
    if any(
        d.active
        and d.status == "confirmed"
        and d.source == "deterministic"
        and (d.old_element == a.id or d.new_element == b.id)
        for d in result.differences
    ):
        return
    for difference in result.differences:
        root = difference.uncertainty_id or difference.id
        if (
            root in unit.targets
            and is_unverified(difference)
            and difference.old_element == a.id
            and difference.new_element == b.id
        ):
            difference.active = False
            result.ai_review.resolutions[root] = "same_independent_ocr"
    result.coverage.old[a.id] = "unchanged"
    result.coverage.new[b.id] = "unchanged"


def _reference_regions(regions, reference, prefix):
    """Возвращает только область, уже переданную модели под этим ref."""
    if reference.startswith(prefix) and reference[1:].isdigit():
        index = int(reference[1:])
        if index < len(regions):
            return [regions[index]]
    return []
