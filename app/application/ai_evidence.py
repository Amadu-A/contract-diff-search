# app/application/ai_evidence.py
"""Независимая привязка цитат ИИ к проверенным словам и локальное разрешение неопределённости."""

import hashlib
from dataclasses import dataclass

from app.domain.comparison import regions
from app.domain.models import ComparisonResult, Difference, ElementMatch, ReviewUnit, VisualRegion
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


def _compatible(result, old_element, new_element, *, visual=False):
    """Контролирует известные стороны договора без требования прежнего алгоритмического match."""
    a = next((e for e in result.old.elements if e.id == old_element), None)
    b = next((e for e in result.new.elements if e.id == new_element), None)
    if a is None or b is None:
        return True
    return all(
        a.attributes.get(key) is None
        or b.attributes.get(key) is None
        or a.attributes[key] == b.attributes[key]
        for key in (("party",) if visual else ("column", "party"))
    )


def _visual_anchor(result, unit, side, value, quote, reference):
    """Проверяет принадлежность исходного ref странице отдельно от правильности OCR."""
    areas = _reference_regions(
        getattr(unit, side + "_regions"), reference, "o" if side == "old" else "n"
    )
    version = getattr(result, side)
    if not areas:
        return None
    region = areas[0]
    page = next((p for p in version.pages if p.number == region.page), None)
    if not page or not (
        0 <= region.x0 < region.x1 <= page.width and 0 <= region.y0 < region.y1 <= page.height
    ):
        return None
    located = anchor(result, unit, side, value, quote, reference)
    if located:
        return located
    elements = [
        e
        for e in version.elements
        if e.id in getattr(unit, side + "_elements")
        and any(_overlap(w.region, region) for w in e.words)
    ]
    return Anchor(elements[0].id if len(elements) == 1 else None, None, areas, False)


def _covered(required, supplied):
    """Проверяет объединение дочерних прямоугольников, включая разрывы между ними."""
    for area in required:
        pieces = [r for r in supplied if _overlap(r, area)]
        xs = sorted(
            {
                area.x0,
                area.x1,
                *(max(area.x0, min(area.x1, x)) for r in pieces for x in (r.x0, r.x1)),
            }
        )
        covered = 0
        for x0, x1 in zip(xs, xs[1:], strict=False):
            intervals = sorted(
                (max(area.y0, r.y0), min(area.y1, r.y1))
                for r in pieces
                if r.x0 <= x0 and r.x1 >= x1
            )
            top, length = area.y0, 0
            for y0, y1 in intervals:
                length += max(0, y1 - max(top, y0))
                top = max(top, y1)
            covered += (x1 - x0) * length
        if covered + 1e-6 < (area.x1 - area.x0) * (area.y1 - area.y0):
            return False
    return bool(required)


def _resolve(result, unit, observation, a, b, same, visual):
    """Накапливает проверенные части и снимает родительское предупреждение после полного покрытия."""
    for warning in result.differences:
        root = warning.uncertainty_id or warning.id
        if not is_unverified(warning) or warning.unverified_reason == "unsupported_property":
            continue
        if unit.targets and root not in unit.targets:
            continue
        localized = any(
            _overlap(r, area)
            for side, located in (("old", a), ("new", b))
            for r in getattr(warning, side + "_regions")
            for area in located.areas
        )
        if not localized:
            continue
        if same and any(
            d.active
            and d.status == "confirmed"
            and d.source == "deterministic"
            and any(
                _overlap(r, area)
                for side, located in (("old", a), ("new", b))
                for r in getattr(d, side + "_regions")
                for area in located.areas
            )
            for d in result.differences
        ):
            warning.unverified_reason = "conflicting_evidence"
            continue
        literal_scope = all(
            not getattr(warning, side + "_text")
            or normalize(getattr(warning, side + "_text")).text
            == normalize(getattr(observation, side + "_value")).text
            for side in ("old", "new")
        )
        local_fact = (
            visual
            and warning.category not in {"unverified_region", "ambiguous_match"}
            and all(
                not getattr(warning, side + "_regions")
                or _covered(getattr(warning, side + "_regions"), located.areas)
                for side, located in (("old", a), ("new", b))
            )
        )
        if not observation.covers_region and not literal_scope and not local_fact:
            continue
        evidence = result.ai_review.covered_regions.setdefault(root, {"old": [], "new": []})
        for side, located in (("old", a), ("new", b)):
            areas = (
                _reference_regions(
                    getattr(unit, side + "_regions"),
                    getattr(observation, side + "_ref"),
                    "o" if side == "old" else "n",
                )
                if observation.covers_region and visual
                else located.areas
            )
            if literal_scope and not visual and located.verified:
                areas = getattr(warning, side + "_regions")
            evidence[side] = list(dict.fromkeys([*evidence[side], *areas]))
        required = {side: getattr(warning, side + "_regions") for side in ("old", "new")}
        if not any(required.values()) or not all(
            not required[side] or _covered(required[side], evidence[side])
            for side in ("old", "new")
        ):
            warning.unverified_reason = "partial_coverage"
            continue
        warning.active = False
        warning.verified_by = "ai_visual" if visual else "ai_and_independent_text"
        result.ai_review.resolutions[root] = "same" if same else "different"
        for side in ("old", "new"):
            identifier = getattr(warning, side + "_element")
            if identifier:
                changed = not same or any(
                    d.active
                    and d.status == "confirmed"
                    and getattr(d, side + "_element") == identifier
                    for d in result.differences
                )
                getattr(result.coverage, side)[identifier] = "changed" if changed else "unchanged"


def merge_reply(result: ComparisonResult, unit: ReviewUnit, reply) -> None:
    """Принимает самостоятельное визуальное доказательство ИИ и сохраняет происхождение факта."""
    unit.old_evidence, unit.new_evidence = reply.old_evidence, reply.new_evidence
    unit.visual_refs = reply.visual_refs
    if reply.outcome == "same" and reply.complete and reply.readable and unit.kind != "coverage":
        _record_contradiction(result, unit)
    _resolve_refined_same(result, unit, reply)
    for observation in [*reply.findings, *reply.evidence]:
        a = _visual_anchor(
            result, unit, "old", observation.old_value, observation.old_quote, observation.old_ref
        )
        b = _visual_anchor(
            result, unit, "new", observation.new_value, observation.new_quote, observation.new_ref
        )
        same = observation.category == "unchanged"
        addition = observation.category.endswith("_added")
        deletion = observation.category.endswith("_deleted")
        visual = bool(
            a
            and b
            and observation.mapping_confirmed
            and observation.mapping_reason.strip()
            and observation.confidence >= 0.8
            and observation.old_ref in reply.visual_refs
            and observation.new_ref in reply.visual_refs
            and (
                observation.category.startswith("graphic_")
                or all(
                    not value or normalize(value).text in normalize(quote).text
                    for value, quote in (
                        (observation.old_value, observation.old_quote),
                        (observation.new_value, observation.new_quote),
                    )
                )
            )
        )
        independent = bool(a and b and a.verified and b.verified and not addition and not deletion)
        confirmed = bool(
            reply.complete
            and reply.readable
            and reply.outcome != "uncertain"
            and a
            and b
            and _compatible(result, a.element, b.element, visual=visual)
            and (visual or independent)
        )
        if addition or deletion:
            confirmed = (
                confirmed
                and visual
                and observation.absence_confirmed
                and observation.covers_region
                and bool(observation.new_value if addition else observation.old_value)
                and not (observation.old_value if addition else observation.new_value)
            )
        elif same:
            confirmed = (
                confirmed
                and normalize(observation.old_value).text == normalize(observation.new_value).text
                and bool(observation.old_value)
            )
        elif observation.category in {
            "formatting_changed",
            "text_moved",
            "table_structure_changed",
            "structure_changed",
            "graphic_modified",
        }:
            confirmed = (
                confirmed and visual and bool(observation.old_value) and bool(observation.new_value)
            )
        else:
            confirmed = (
                confirmed
                and normalize(observation.old_value).text != normalize(observation.new_value).text
            )
        if observation.category == "unverified_region":
            confirmed = False
        if same:
            if confirmed:
                if any(
                    d.active
                    and d.source == "deterministic"
                    and d.status == "confirmed"
                    and any(
                        _overlap(r, area)
                        for side, located in (("old", a), ("new", b))
                        for r in getattr(d, side + "_regions")
                        for area in located.areas
                    )
                    for d in result.differences
                ):
                    continue
                _save_mapping(result, unit, observation, a, b, same=True)
                unit.verified_by = "ai_visual" if visual else "ai_and_independent_text"
                _resolve(result, unit, observation, a, b, True, visual)
            continue
        ar = (
            []
            if addition
            else a.areas
            if a
            else _reference_regions(unit.old_regions, observation.old_ref, "o")
        )
        br = (
            []
            if deletion
            else b.areas
            if b
            else _reference_regions(unit.new_regions, observation.new_ref, "n")
        )
        fingerprint = (
            observation.category,
            observation.old_value,
            observation.new_value,
            [(r.page, r.x0, r.y0, r.x1, r.y1) for r in ar + br],
        )
        candidate = Difference(
            "AI" + hashlib.sha256(repr(fingerprint).encode()).hexdigest()[:16],
            observation.category,
            "ИИ: найдено различие" if confirmed else "ИИ: требуется проверка",
            observation.old_value,
            observation.new_value,
            old_regions=ar,
            new_regions=br,
            old_element=a.element if a else None,
            new_element=b.element if b else None,
            old_range=a.span if a and not addition else None,
            new_range=b.span if b and not deletion else None,
            status="confirmed" if confirmed else "needs_review",
            source="ai",
            reason=". ".join(
                dict.fromkeys(filter(None, (observation.reason, observation.mapping_reason)))
            ),
            ai_mode=unit.mode,
            verified_by=("ai_visual" if visual else "ai_and_independent_text") if confirmed else "",
            confidence=observation.confidence,
            unverified_reason=""
            if confirmed
            else "partial_coverage"
            if not reply.complete
            else "unreadable_image"
            if not reply.readable
            else "mapping_not_found",
            old_ocr=unit.old_text if a and a.span is None else "",
            new_ocr=unit.new_text if b and b.span is None else "",
            old_anchor_regions=a.areas if addition and a else [],
            new_anchor_regions=b.areas if deletion and b else [],
            uncertainty_id=unit.targets[0]
            if observation.category == "unverified_region" and len(unit.targets) == 1
            else "",
        )
        if (
            confirmed
            and candidate.category in {"number_modified", "text_modified", "table_modified"}
            and any(
                d.active
                and d.source == "deterministic"
                and d.status == "confirmed"
                and normalize(d.old_text).text == normalize(candidate.old_text).text
                and normalize(d.new_text).text != normalize(candidate.new_text).text
                and any(_overlap(x, y) for x in d.old_regions for y in candidate.old_regions)
                and any(_overlap(x, y) for x in d.new_regions for y in candidate.new_regions)
                for d in result.differences
            )
        ):
            confirmed = False
            candidate.status, candidate.verified_by, candidate.unverified_reason = (
                "needs_review",
                "",
                "conflicting_evidence",
            )
            candidate.title = "ИИ: противоречивое значение требует проверки"
        for side, located in (("old", a), ("new", b)):
            element = next(
                (e for e in getattr(result, side).elements if located and e.id == located.element),
                None,
            )
            if element:
                setattr(candidate, side + "_section", element.section)
        duplicates = [d for d in result.differences if _duplicate(d, candidate)]
        if duplicates:
            for difference in duplicates:
                if (
                    confirmed
                    and difference.status == "confirmed"
                    and difference.source == "deterministic"
                ):
                    difference.verified_by = "algorithms_and_ai"
                elif confirmed:
                    if is_unverified(difference):
                        result.ai_review.resolutions[difference.uncertainty_id or difference.id] = (
                            "different"
                        )
                    difference.old_ocr, difference.new_ocr = (
                        difference.old_text,
                        difference.new_text,
                    )
                    difference.status, difference.source = "confirmed", "ai"
                    difference.verified_by, difference.ai_mode = candidate.verified_by, unit.mode
                    difference.old_regions, difference.new_regions = ar, br
                    difference.old_range, difference.new_range = (
                        candidate.old_range,
                        candidate.new_range,
                    )
                    difference.unverified_reason = ""
        elif not any(d.id == candidate.id for d in result.differences):
            result.differences.append(candidate)
        if confirmed:
            unit.verified_by = candidate.verified_by
            _save_mapping(result, unit, observation, a, b)
            for side, located in (("old", a), ("new", b)):
                if located.element:
                    getattr(result.coverage, side)[located.element] = "changed"
            _resolve(result, unit, observation, a, b, False, visual)
            for conflict in result.differences:
                if (
                    conflict.category == "ai_contradiction"
                    and conflict.active
                    and any(
                        _duplicate(d, candidate) and conflict.id == "AI-C-" + d.id
                        for d in duplicates
                    )
                ):
                    conflict.active = False
    remaining = uncertainty_ids(result.differences)
    result.coverage.resolved_ai = len(set(result.ai_review.original_uncertainties) - remaining)
    result.coverage.new_unverified = len(remaining - set(result.ai_review.original_uncertainties))
    result.coverage.geometry_missing = sum(
        int(bool(d.old_element and not d.old_regions and not d.category.endswith("_added")))
        + int(bool(d.new_element and not d.new_regions and not d.category.endswith("_deleted")))
        for d in result.differences
        if d.active
    )
    if not result.coverage.geometry_missing:
        result.warnings = [
            warning
            for warning in result.warnings
            if warning != "Не все изменения имеют надёжную геометрическую привязку"
        ]
    result.ai_review.resolutions = {
        key: value for key, value in result.ai_review.resolutions.items() if key not in remaining
    }


def _save_mapping(result, unit, observation, a, b, same=False):
    """Сохраняет соответствие из изображений, в том числе без алгоритмического якоря."""
    method = (
        ("ai_visual_same" if same else "ai_visual")
        if observation.mapping_confirmed and unit.visual_refs
        else "ai_and_independent_text"
    )
    if not any(
        m.source == "ai" and m.old_regions == a.areas and m.new_regions == b.areas
        for m in result.matches
    ):
        result.matches.append(
            ElementMatch(
                [a.element] if a.element else [],
                [b.element] if b.element else [],
                method,
                observation.confidence,
                observation.category == "text_moved",
                a.areas,
                b.areas,
                "ai",
                observation.mapping_reason or "Сопоставлено ИИ с исходными областями",
            )
        )


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
    if not _compatible(result, a.id, b.id):
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
            and difference.unverified_reason != "unsupported_property"
            and is_unverified(difference)
            and difference.old_element == a.id
            and difference.new_element == b.id
        ):
            difference.active = False
            difference.verified_by = "ai_and_independent_text"
            result.ai_review.resolutions[root] = "same_independent_ocr"
            unit.verified_by = "ai_and_independent_text"
    if unit.verified_by and not any(
        m.source == "ai" and a.id in m.old and b.id in m.new for m in result.matches
    ):
        result.matches.append(
            ElementMatch(
                [a.id],
                [b.id],
                "ai_and_independent_text",
                1,
                old_regions=unit.old_regions,
                new_regions=unit.new_regions,
                source="ai",
                reason="Совпали полные независимые OCR-кропы и визуальный вывод ИИ",
            )
        )
    result.coverage.old[a.id] = "unchanged"
    result.coverage.new[b.id] = "unchanged"


def _reference_regions(regions, reference, prefix):
    """Возвращает только область, уже переданную модели под этим ref."""
    if reference.startswith(prefix) and reference[1:].isdigit():
        index = int(reference[1:])
        if index < len(regions):
            return [regions[index]]
    return []
