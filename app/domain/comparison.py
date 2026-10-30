# app/domain/comparison.py
"""Сравнение сопоставленных элементов и полный учёт непроверенных областей."""

import re
from collections import Counter

from app.domain.algorithms import damerau_levenshtein, myers
from app.domain.matching import match_elements
from app.domain.models import (
    ComparisonResult,
    CoverageReport,
    Difference,
    DocumentElement,
    DocumentVersion,
    VisualRegion,
)
from app.domain.text import NUMBER, content, excluded_pair, normalize, tokens
from app.domain.visibility import is_unverified, uncertainty_ids


def regions(element: DocumentElement, start: int = 0, end: int | None = None) -> list[VisualRegion]:
    """Выбирает области конкретных слов; OCR не выдаётся за точные границы символов."""
    end = len(element.text) if end is None else end
    result = []
    for word in element.words:
        if word.end > start and word.start < end and word.region not in result:
            if word.characters and len(word.characters) == len(word.text):
                result.extend(
                    word.characters[
                        max(0, start - word.start) : min(len(word.text), end - word.start)
                    ]
                )
            else:
                result.append(word.region)
    return result


def _text_differences(
    old: DocumentElement,
    new: DocumentElement,
    ocr_involved: bool = False,
    exclusions: frozenset[str] = frozenset(),
) -> list[Difference]:
    """Сравнивает локальные токены, не теряя проценты, отрицания и пунктуацию."""
    ta, tb = tokens(old.text), tokens(new.text)
    # Сравнение исходного отображения после безопасной нормализации пробелов.
    edits = myers(
        [
            normalize(t[0]).text.replace(" ", "") if t[0][0].isdigit() else normalize(t[0]).text
            for t in ta
        ],
        [
            normalize(t[0]).text.replace(" ", "") if t[0][0].isdigit() else normalize(t[0]).text
            for t in tb
        ],
    )
    result, deleted, inserted = [], [], []

    def flush() -> None:
        """Формирует одно независимое изменение для непрерывной группы токенов."""
        if not deleted and not inserted:
            return
        ar = (ta[deleted[0]][1], ta[deleted[-1]][2]) if deleted else None
        br = (tb[inserted[0]][1], tb[inserted[-1]][2]) if inserted else None
        before = old.text[slice(*ar)] if ar else ""
        after = new.text[slice(*br)] if br else ""
        if (not old.section or not new.section or old.section == new.section) and excluded_pair(
            old.text, new.text, ar, br, exclusions
        ):
            deleted.clear()
            inserted.clear()
            return
        numeric = bool(re.search(r"\d", before + after))
        category = "number_modified" if numeric and ar and br else "text_modified"
        if ar is None:
            category = "text_added"
        elif br is None:
            category = "text_deleted"
        cell_change = old.kind == new.kind == "cell" and category in {
            "text_modified",
            "number_modified",
        }
        original_category = category
        if cell_change:
            category = "table_modified"
        if before and after and max(len(before), len(after)) <= 512:
            distance = damerau_levenshtein(before, after)
            if distance <= 2 and ar and br and len(deleted) == len(inserted) == 1:
                prefix = 0
                while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
                    prefix += 1
                suffix = 0
                while (
                    suffix < min(len(before), len(after)) - prefix
                    and before[-1 - suffix] == after[-1 - suffix]
                ):
                    suffix += 1
                if prefix + suffix < min(len(before), len(after)):
                    ar = (ar[0] + prefix, ar[1] - suffix)
                    br = (br[0] + prefix, br[1] - suffix)
        confidence = min(old.confidence, new.confidence)
        changed_words = [
            w
            for e, span in ((old, ar), (new, br))
            if span
            for w in e.words
            if w.end > span[0] and w.start < span[1]
        ]
        uncertain = confidence < 0.65 or any(
            w.confidence < 0.65 or not w.verified for w in changed_words
        )
        if ocr_involved and before and after:
            punctuation = not any(char.isalnum() for char in before + after)
            missing_digit = numeric and not (re.search(r"\d", before) and re.search(r"\d", after))
            short_letter = not numeric and (
                max(len(before), len(after)) <= 2
                or max(len(before), len(after)) <= 512
                and damerau_levenshtein(before, after) <= 1
            )
            uncertain = uncertain or punctuation or missing_digit or short_letter
        result.append(
            Difference(
                "",
                category,
                f"Пункт {old.section or new.section or 'без номера'} — {'число' if numeric else 'текст'}",
                before,
                after,
                old.section,
                new.section,
                regions(old, *ar) if ar else [],
                regions(new, *br) if br else [],
                old.id,
                new.id,
                ar,
                br,
                "unverified" if uncertain else "confirmed",
                reason="Требуется проверка распознавания" if uncertain else "",
            )
        )
        if cell_change:
            result[-1].subcategories.append(original_category)
        deleted.clear()
        inserted.clear()

    for action, i, j in edits:
        if action == "equal":
            flush()
        elif action == "delete":
            deleted.append(i)
        else:
            inserted.append(j)
    flush()
    return result


def compare(
    old: DocumentVersion,
    new: DocumentVersion,
    visual_scores: dict[tuple[str, str], float] | None = None,
    exclusions: frozenset[str] = frozenset(),
) -> ComparisonResult:
    """Выполняет структурное сравнение; каждый узел получает итоговую классификацию."""
    matches, ambiguous_a, ambiguous_b = match_elements(old.elements, new.elements, exclusions)
    oa, nb = {e.id: e for e in old.elements}, {e.id: e for e in new.elements}
    coverage_a, coverage_b, differences = {}, {}, []
    compare_format = (
        old.format in {"doc", "docx"}
        and new.format in {"doc", "docx"}
        or (
            old.format == new.format == "pdf"
            and all(p.mode == "text" for v in (old, new) for p in v.pages)
        )
    )
    for match in matches:
        if match.method == "comparison_exclusion":
            coverage_a[match.old[0]] = coverage_b[match.new[0]] = "unchanged"
            continue
        before, after = oa[match.old[0]], nb[match.new[0]]
        changes = []
        if (len(match.old) > 1 or len(match.new) > 1) and old.format == new.format:
            changes.append(
                Difference(
                    "",
                    "structure_changed",
                    "Разбиение или объединение абзацев",
                    " ".join(oa[i].text for i in match.old),
                    " ".join(nb[i].text for i in match.new),
                    before.section,
                    after.section,
                    [r for i in match.old for r in regions(oa[i])],
                    [r for i in match.new for r in regions(nb[i])],
                    before.id,
                    after.id,
                )
            )
        elif len(match.old) == 1 and len(match.new) == 1:
            ocr_involved = any(
                page.mode in {"scanned", "mixed", "visual_ocr"}
                for version in (old, new)
                for page in version.pages
            )
            changes.extend(_text_differences(before, after, ocr_involved, exclusions))
            old_number, new_number = NUMBER.match(before.text), NUMBER.match(after.text)
            if (
                old_number
                and new_number
                and old_number.group().strip() != new_number.group().strip()
                and content(before.text) == content(after.text)
            ):
                changes = [
                    c for c in changes if not c.old_range or c.old_range[0] >= old_number.end()
                ]
                changes.append(
                    Difference(
                        "",
                        "numbering_changed",
                        "Изменена нумерация",
                        old_number.group().strip(),
                        new_number.group().strip(),
                        before.section,
                        after.section,
                        regions(before, 0, old_number.end()),
                        regions(after, 0, new_number.end()),
                        before.id,
                        after.id,
                    )
                )
            if before.attributes.get("hyperlinks", "") != after.attributes.get("hyperlinks", ""):
                changes.append(
                    Difference(
                        "",
                        "text_modified"
                        if old.format in {"doc", "docx"} and new.format in {"doc", "docx"}
                        else "unverified_region",
                        "Адрес гиперссылки"
                        if "pdf" in {old.format, new.format}
                        else "Изменён адрес гиперссылки",
                        before.attributes.get("hyperlinks", ""),
                        after.attributes.get("hyperlinks", ""),
                        before.section,
                        after.section,
                        regions(before),
                        regions(after),
                        before.id,
                        after.id,
                        status="unverified" if "pdf" in {old.format, new.format} else "confirmed",
                        reason="Невозможно проверить адрес гиперссылки по сканированному PDF"
                        if "pdf" in {old.format, new.format}
                        else "",
                        unverified_reason="unsupported_property"
                        if "pdf" in {old.format, new.format}
                        else "",
                    )
                )
            if (
                compare_format
                and before.runs != after.runs
                and content(before.text) == content(after.text)
                and _style_signature(before) != _style_signature(after)
            ):
                # Сдвиг диапазонов из-за текста не является отдельным форматным событием.
                changes.append(
                    Difference(
                        "",
                        "formatting_changed",
                        "Изменено оформление",
                        before.text,
                        after.text,
                        before.section,
                        after.section,
                        regions(before),
                        regions(after),
                        before.id,
                        after.id,
                    )
                )
            if (
                (
                    before.kind != after.kind
                    or before.attributes.get("grid", "") != after.attributes.get("grid", "")
                )
                and old.format in {"doc", "docx"}
                and new.format in {"doc", "docx"}
            ):
                category = (
                    "table_structure_changed"
                    if "cell" in (before.kind, after.kind)
                    else "structure_changed"
                )
                changes.append(
                    Difference(
                        "",
                        category,
                        "Изменена структура",
                        before.kind,
                        after.kind,
                        before.section,
                        after.section,
                        regions(before),
                        regions(after),
                        before.id,
                        after.id,
                    )
                )
        if match.moved:
            changes.append(
                Difference(
                    "",
                    "text_moved",
                    "Перемещён фрагмент",
                    before.text,
                    after.text,
                    before.section,
                    after.section,
                    regions(before),
                    regions(after),
                    before.id,
                    after.id,
                )
            )
        state = "changed" if changes else "unchanged"
        if min(before.confidence, after.confidence) < 0.65 or any(
            w.confidence < 0.65 or not w.verified
            for e in (before, after)
            for w in e.words
            if re.search(r"\d", w.text)
        ):
            state = "unverified"
        for i in match.old:
            coverage_a[i] = state
        for i in match.new:
            coverage_b[i] = state
        differences.extend(changes)

    for elements, coverage, ambiguous, side in (
        (old.elements, coverage_a, ambiguous_a, "old"),
        (new.elements, coverage_b, ambiguous_b, "new"),
    ):
        for element in elements:
            if element.id in coverage:
                if coverage[element.id] != "unverified":
                    continue
                category, state = "unverified_region", "unverified"
            elif element.id in ambiguous:
                category, state = "ambiguous_match", "ambiguous"
            elif element.confidence < 0.65 or _relevant_warning(element, side, old, new, matches):
                category, state = "unverified_region", "unverified"
            else:
                category, state = ("text_deleted" if side == "old" else "text_added"), "changed"
            coverage[element.id] = state
            differences.append(
                Difference(
                    "",
                    category,
                    f"Пункт {element.section or 'без номера'} — "
                    + {
                        "text_added": "добавлен фрагмент",
                        "text_deleted": "удалён фрагмент",
                        "unverified_region": "непроверенная область",
                        "ambiguous_match": "неоднозначное соответствие",
                    }[category],
                    old_text=element.text if side == "old" else "",
                    new_text=element.text if side == "new" else "",
                    old_section=element.section if side == "old" else "",
                    new_section=element.section if side == "new" else "",
                    old_regions=regions(element) if side == "old" else [],
                    new_regions=regions(element) if side == "new" else [],
                    old_element=element.id if side == "old" else None,
                    new_element=element.id if side == "new" else None,
                    status="unverified" if state == "unverified" else "confirmed",
                    reason="Место на отсутствующей стороне не установлено"
                    if state == "changed"
                    else "Недостаточно доказательств",
                )
            )
    for side, version in (("old", old), ("new", new)):
        for page in version.pages:
            for warning in page.warnings:
                rect = VisualRegion(page.number, 0, 0, page.width, page.height)
                differences.append(
                    Difference(
                        "",
                        "unverified_region",
                        f"Страница {page.number} — непроверенная область",
                        old_regions=[rect] if side == "old" else [],
                        new_regions=[rect] if side == "new" else [],
                        status="unverified",
                        reason=warning,
                    )
                )

    differences.extend(_graphics(old, new, visual_scores or {}))
    for side, document, other in (("old", old, new), ("new", new, old)):
        if document.format not in {"doc", "docx"} or other.format != "pdf":
            continue
        for element in document.elements:
            if not element.attributes.get("hidden_fields"):
                continue
            differences.append(
                Difference(
                    "",
                    "unverified_region",
                    "Скрытые поля Word — непроверяемое свойство",
                    old_element=element.id if side == "old" else None,
                    new_element=element.id if side == "new" else None,
                    old_regions=regions(element) if side == "old" else [],
                    new_regions=regions(element) if side == "new" else [],
                    status="unverified",
                    reason="Невозможно проверить скрытые поля Word по сканированному PDF",
                    unverified_reason="unsupported_property",
                )
            )
    _group_uncertainties(differences, matches)
    for version, coverage, side in ((old, coverage_a, "old"), (new, coverage_b, "new")):
        for graphic in version.graphics:
            affected = [d for d in differences if getattr(d, side + "_element") == graphic.id]
            coverage[graphic.id] = (
                "unverified"
                if graphic.confidence < 0.65
                else "changed"
                if affected
                else "unchanged"
            )
    for i, difference in enumerate(differences, 1):
        difference.id = f"D{i:05d}"
    missing = sum(
        bool(d.old_element and not d.old_regions) + bool(d.new_element and not d.new_regions)
        for d in differences
        if d.active
    )
    coverage = CoverageReport(
        coverage_a,
        coverage_b,
        {"old": [p.number for p in old.pages], "new": [p.number for p in new.pages]},
        missing,
        len(uncertainty_ids(differences)),
    )
    return ComparisonResult(
        old,
        new,
        differences,
        matches,
        coverage,
        warnings=old.warnings
        + new.warnings
        + (["Не все изменения имеют надёжную геометрическую привязку"] if missing else []),
        comparison_exclusions=sorted(exclusions),
    )


def _relevant_warning(element, side, old, new, matches) -> bool:
    """Ограничивает предупреждение соответствующим разделом/соседними структурными опорами."""
    own, other = (old, new) if side == "old" else (new, old)
    pages = {
        w.region.page
        for e in other.elements
        if element.section and e.section == element.section
        for w in e.words
    }
    if not pages:
        order = {e.id: i for i, e in enumerate(own.elements)}
        opposite = {e.id: e for e in other.elements}
        anchors = sorted(
            [
                (min(abs(order.get(i, -10000) - order[element.id]) for i in getattr(m, side)), m)
                for m in matches
                if getattr(m, side)
            ],
            key=lambda item: item[0],
        )
        for _, match in anchors[:2]:
            for identifier in match.new if side == "old" else match.old:
                if identifier in opposite:
                    pages.update(w.region.page for w in opposite[identifier].words)
    if not other.elements:
        return any(p.warnings for p in other.pages)
    return any(p.warnings for p in other.pages if p.number in pages)


def _group_uncertainties(differences, matches) -> None:
    """Объединяет дубли предупреждений одного соответствия без потери локальных изменений."""
    owners = {}
    for index, match in enumerate(matches):
        for side in ("old", "new"):
            for identifier in getattr(match, side):
                owners[(side, identifier)] = f"pair-{index}"
    warnings = {}
    for difference in differences:
        if not is_unverified(difference):
            continue
        if not difference.unverified_reason:
            difference.unverified_reason = (
                "missing_geometry"
                if (difference.old_element and not difference.old_regions)
                or (difference.new_element and not difference.new_regions)
                else "mapping_not_found"
            )
        roots = sorted(
            {
                owners.get(
                    (side, getattr(difference, side + "_element")),
                    f"{side}:{getattr(difference, side + '_element')}",
                )
                for side in ("old", "new")
                if getattr(difference, side + "_element")
            }
        )
        if not roots:
            roots = [
                f"{side}:page:{r.page}"
                for side in ("old", "new")
                for r in getattr(difference, side + "_regions")
            ]
        difference.uncertainty_id = "|".join(roots) or "unknown-area"
        if difference.unverified_reason == "unsupported_property":
            difference.uncertainty_id += ":property:" + difference.title
        if difference.category != "unverified_region":
            continue
        previous = warnings.get(difference.uncertainty_id)
        if previous is None:
            warnings[difference.uncertainty_id] = difference
            continue
        for side in ("old", "new"):
            areas = getattr(previous, side + "_regions")
            areas.extend(r for r in getattr(difference, side + "_regions") if r not in areas)
            if not getattr(previous, side + "_element"):
                setattr(previous, side + "_element", getattr(difference, side + "_element"))
                setattr(previous, side + "_text", getattr(difference, side + "_text"))
        if difference.reason and difference.reason not in previous.reason:
            previous.reason = "; ".join(filter(None, (previous.reason, difference.reason)))
        difference.active = False
    for root, warning in warnings.items():
        candidates = [
            d
            for d in differences
            if d.active
            and is_unverified(d)
            and d.uncertainty_id == root
            and d.category != "unverified_region"
        ]
        if candidates:
            warning.active = False
            for candidate in candidates:
                candidate.reason = "; ".join(
                    dict.fromkeys(filter(None, (candidate.reason, warning.reason)))
                )


def _style_signature(element: DocumentElement) -> tuple:
    """Сравнивает оформление символов, не считая техническое разбиение runs изменением."""
    styles = [()] * len(element.text)
    for run in element.runs:
        styles[max(0, run.start) : min(len(styles), run.end)] = [run.properties] * max(
            0, min(len(styles), run.end) - max(0, run.start)
        )
    return tuple(styles[i] for i, char in enumerate(element.text) if not char.isspace())


def _graphics(
    old: DocumentVersion, new: DocumentVersion, visual_scores: dict[tuple[str, str], float]
) -> list[Difference]:
    """Сопоставляет локальные объекты; схожий pHash не доказывает идентичность."""
    result, used = [], set()
    for a in old.graphics:
        exact = [j for j, b in enumerate(new.graphics) if j not in used and a.digest == b.digest]
        close = [
            j
            for j, b in enumerate(new.graphics)
            if j not in used
            and (a.confidence >= 0.65) == (b.confidence >= 0.65)
            and (int(a.perceptual_hash, 16) ^ int(b.perceptual_hash, 16)).bit_count() <= 12
            and visual_scores.get((a.id, b.id), 1) >= 0.2
        ]
        found = exact if len(exact) == 1 else close
        if len(found) == 1:
            j, b = found[0], new.graphics[found[0]]
            used.add(j)
            if a.digest != b.digest:
                category = "graphic_modified"
            elif (
                a.region.page != b.region.page
                or abs(a.region.x0 - b.region.x0) + abs(a.region.y0 - b.region.y0) > 40
            ):
                category = "graphic_moved"
            else:
                continue
            result.append(
                Difference(
                    "",
                    category,
                    "Изменён графический объект",
                    a.description,
                    b.description,
                    old_regions=[a.region],
                    new_regions=[b.region],
                    old_element=a.id,
                    new_element=b.id,
                    status="unverified" if min(a.confidence, b.confidence) < 0.65 else "confirmed",
                    reason=f"Локальный SSIM: {visual_scores[(a.id, b.id)]:.4f}; сходство не доказывает идентичность"
                    if (a.id, b.id) in visual_scores
                    else "",
                )
            )
        elif len(found) > 1:
            result.append(
                Difference(
                    "",
                    "ambiguous_match",
                    "Неоднозначная графика",
                    a.description,
                    old_regions=[a.region],
                    status="unverified",
                    old_element=a.id,
                )
            )
        else:
            result.append(
                Difference(
                    "",
                    "unverified_region" if a.confidence < 0.65 else "graphic_deleted",
                    "Удалён графический объект",
                    a.description,
                    old_regions=[a.region],
                    old_element=a.id,
                    status="unverified" if a.confidence < 0.65 else "confirmed",
                )
            )
    for j, b in enumerate(new.graphics):
        if j not in used:
            result.append(
                Difference(
                    "",
                    "unverified_region" if b.confidence < 0.65 else "graphic_added",
                    "Добавлены печать, подпись или графика",
                    new_text=b.description,
                    new_regions=[b.region],
                    new_element=b.id,
                    status="confirmed" if b.confidence >= 0.65 else "unverified",
                )
            )
    return result


def statistics(differences: list[Difference]) -> dict[str, int]:
    """Считает уникальные события; замечания ИИ и пробелы покрытия не входят в total."""
    active = [d for d in differences if d.active]
    confirmed = [d for d in active if d.status == "confirmed" and not is_unverified(d)]
    counts = Counter(d.category for d in confirmed)
    excluded = {"unverified_region", "ambiguous_match"}
    return {
        "total": sum(d.category not in excluded for d in confirmed),
        "added": counts["text_added"] + counts["graphic_added"],
        "deleted": counts["text_deleted"] + counts["graphic_deleted"],
        "ambiguous": sum(d.category == "ambiguous_match" for d in active),
        "unverified": len(uncertainty_ids(active)),
        "ai_needs_review": sum(d.source == "ai" and d.status == "needs_review" for d in active),
        "ai_confirmed": sum(d.verified_by.startswith("ai") or d.source == "ai" for d in confirmed),
        "algorithms_confirmed": sum(d.source == "deterministic" for d in confirmed),
        "both_confirmed": sum(d.verified_by == "algorithms_and_ai" for d in confirmed),
    }
