# app/application/ai_planning.py
"""Полный план страниц/блоков и приоритетный план действительно непроверенных областей."""

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict
from functools import partial

from app.domain.models import (
    ComparisonResult,
    DocumentElement,
    DocumentVersion,
    ReviewUnit,
    VisualRegion,
)
from app.domain.text import content, tokens
from app.domain.visibility import is_unverified


def element_regions(elements: list[DocumentElement]) -> list[VisualRegion]:
    """Объединяет реальные слова каждого блока на странице, не придумывая геометрию."""
    groups = defaultdict(list)
    for element in elements:
        for word in element.words:
            groups[(element.id, word.region.page)].append(word.region)
    return [
        VisualRegion(
            key[1],
            min(r.x0 for r in items),
            min(r.y0 for r in items),
            max(r.x1 for r in items),
            max(r.y1 for r in items),
        )
        for key, items in groups.items()
    ]


def candidates(source: list[DocumentElement], opposite: DocumentVersion) -> list[DocumentElement]:
    """Ищет контекст по разделу, колонке и редким словам во всём другом документе."""
    words = {t.casefold() for t, _, _ in tokens(content(" ".join(e.text for e in source)))}
    sections = {e.section for e in source if e.section}
    columns = {e.attributes.get("column") for e in source if e.attributes.get("column") is not None}
    ranked = []
    for element in opposite.elements:
        own = {t.casefold() for t, _, _ in tokens(content(element.text))}
        score = (4 if element.section in sections else 0) + len(words & own) / max(
            1, len(words | own)
        )
        if element.attributes.get("column") in columns:
            score += 0.3
        if score > 0:
            ranked.append((score, element))
    return [element for _, element in sorted(ranked, key=lambda pair: -pair[0])[:3]]


def _page_areas(document, elements, surface=None):
    """Объединяет видимые области по страницам и сохраняет целиком абзацы на границах полос."""
    groups = defaultdict(list)
    for region in element_regions(elements):
        groups[region.page].append(region)
    if surface is not None:
        groups[surface.page].append(surface)
    pages = {page.number: page for page in document.pages}
    return [
        VisualRegion(
            number,
            0,
            max(0, min(r.y0 for r in areas)),
            pages[number].width,
            min(pages[number].height, max(r.y1 for r in areas)),
        )
        for number, areas in sorted(groups.items())
    ]


def unit_id(unit: ReviewUnit) -> str:
    """Даёт стабильный идентификатор по контексту, области и версии промпта без текста в логах."""
    values = {
        key: getattr(unit, key)
        for key in (
            "mode",
            "kind",
            "old_elements",
            "new_elements",
            "old_text",
            "new_text",
            "prompt_version",
            "segment",
        )
    }
    values.update(
        old_regions=[asdict(r) for r in unit.old_regions],
        new_regions=[asdict(r) for r in unit.new_regions],
    )
    return "U" + hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:20]


def _make_units(
    mode, kind, old, new, limit, targets=(), old_regions=None, new_regions=None, region_limit=6
):
    """Делит длинные тексты без потери хвоста; каждый блок получает исходные визуальные области."""
    old_text, new_text = "\n".join(e.text for e in old), "\n".join(e.text for e in new)
    old_parts = [old_text[i : i + limit] for i in range(0, len(old_text), limit)] or [""]
    new_parts = [new_text[i : i + limit] for i in range(0, len(new_text), limit)] or [""]
    result = []
    ar = element_regions(old) if old_regions is None else old_regions
    br = element_regions(new) if new_regions is None else new_regions
    a_groups = [ar[i : i + region_limit] for i in range(0, len(ar), region_limit)] or [[]]
    b_groups = [br[i : i + region_limit] for i in range(0, len(br), region_limit)] or [[]]
    segments = max(len(old_parts), len(new_parts))
    for group in range(max(len(a_groups), len(b_groups))):
        for index in range(segments):
            unit = ReviewUnit(
                "",
                mode,
                kind,
                [e.id for e in old],
                [e.id for e in new],
                a_groups[group % len(a_groups)],
                b_groups[group % len(b_groups)],
                old_parts[index] if index < len(old_parts) else "",
                new_parts[index] if index < len(new_parts) else "",
                list(targets),
            )
            unit.segment = group * segments + index
            unit.id = unit_id(unit)
            result.append(unit)
    return result


def _kind(elements, fallback="text") -> str:
    """Выбирает отдельные инструкции OCR, таблиц и критических числовых изменений."""
    if any(e.confidence < 0.65 or any(not w.verified for w in e.words) for e in elements):
        return "ocr"
    if any(e.kind == "cell" for e in elements):
        return "table"
    if any(re.search(r"\d|%", e.text) for e in elements):
        return "number"
    return fallback


def full_plan(
    result: ComparisonResult, context_chars: int, region_limit: int = 6
) -> list[ReviewUnit]:
    """Планирует карту разделов, все страницы с перекрытием и все сопоставленные/несопоставленные блоки."""
    make_units = partial(_make_units, region_limit=region_limit)
    old, new = {e.id: e for e in result.old.elements}, {e.id: e for e in result.new.elements}
    units, used_old, used_new = [], set(), set()
    sections = sorted({e.section for e in [*old.values(), *new.values()]})
    for section in sections:
        a = [e for e in old.values() if e.section == section]
        b = [e for e in new.values() if e.section == section]
        # Карта видимых структурных опор, а не готовая матрица различий.
        headers_a = [
            DocumentElement(
                e.id, e.kind, f"{e.id} {e.kind}: {e.text[:100]}", e.section, words=e.words
            )
            for e in a
        ]
        headers_b = [
            DocumentElement(
                e.id, e.kind, f"{e.id} {e.kind}: {e.text[:100]}", e.section, words=e.words
            )
            for e in b
        ]
        units.extend(make_units("full", "correspondence", headers_a, headers_b, context_chars))
    for match in result.matches:
        a, b = [old[i] for i in match.old], [new[i] for i in match.new]
        used_old.update(match.old)
        used_new.update(match.new)
        units.extend(make_units("full", _kind(a + b), a, b, context_chars))
    for side, source, other, used in (
        ("old", result.old, result.new, used_old),
        ("new", result.new, result.old, used_new),
    ):
        for element in source.elements:
            if element.id in used:
                continue
            opposite = candidates([element], other)
            a, b = ([element], opposite) if side == "old" else (opposite, [element])
            units.extend(make_units("full", "correspondence", a, b, context_chars))
        for page in source.pages:
            # Полное покрытие поверхности страницы, в том числе областей вне OCR и неизвестной графики.
            for tile in range(3):
                area = VisualRegion(
                    page.number,
                    0,
                    max(0, page.height * (tile / 3 - 0.015)),
                    page.width,
                    min(page.height, page.height * ((tile + 1) / 3 + 0.015)),
                )
                elements = [
                    e
                    for e in source.elements
                    if any(
                        w.region.page == page.number
                        and w.region.y1 > area.y0
                        and w.region.y0 < area.y1
                        for w in e.words
                    )
                ]
                opposite_ids = set()
                for match in result.matches:
                    own = match.old if side == "old" else match.new
                    if any(e.id in own for e in elements):
                        opposite_ids.update(match.new if side == "old" else match.old)
                opposite_map = new if side == "old" else old
                opposite = [opposite_map[i] for i in sorted(opposite_ids)] or candidates(
                    elements, other
                )
                a, b = (elements, opposite) if side == "old" else (opposite, elements)
                ar, br = (
                    (_page_areas(source, elements, area), _page_areas(other, opposite))
                    if side == "old"
                    else (_page_areas(other, opposite), _page_areas(source, elements, area))
                )
                units.extend(
                    make_units("full", "page", a, b, context_chars, old_regions=ar, new_regions=br)
                )
        for graphic in source.graphics:
            same_page = [
                e
                for e in source.elements
                if any(w.region.page == graphic.region.page for w in e.words)
            ]
            opposite = candidates(same_page[-3:], other)
            a, b = (same_page[-3:], opposite) if side == "old" else (opposite, same_page[-3:])
            ar, br = (
                ([graphic.region], element_regions(opposite))
                if side == "old"
                else (element_regions(opposite), [graphic.region])
            )
            units.extend(
                make_units("full", "graphic", a, b, context_chars, old_regions=ar, new_regions=br)
            )
    return list({unit.id: unit for unit in units}.values())


def unverified_plan(
    result: ComparisonResult, context_chars: int, region_limit: int = 6
) -> list[ReviewUnit]:
    """Планирует все оставшиеся корневые неопределённости, начиная с OCR и геометрии."""
    groups = defaultdict(list)
    for difference in result.differences:
        if is_unverified(difference):
            groups[difference.uncertainty_id or difference.id].append(difference)
    old, new = {e.id: e for e in result.old.elements}, {e.id: e for e in result.new.elements}
    ranked = []
    for root, differences in groups.items():
        a = [old[i] for i in dict.fromkeys(d.old_element for d in differences) if i in old]
        b = [new[i] for i in dict.fromkeys(d.new_element for d in differences) if i in new]
        for match in result.matches:
            if any(e.id in match.old for e in a) or any(e.id in match.new for e in b):
                a = list({e.id: e for e in [*a, *(old[i] for i in match.old)]}.values())
                b = list({e.id: e for e in [*b, *(new[i] for i in match.new)]}.values())
        if a and not b:
            b = candidates(a, result.new)
        elif b and not a:
            a = candidates(b, result.old)
        kind = _kind(a + b)
        missing = any(
            (d.old_element and not d.old_regions) or (d.new_element and not d.new_regions)
            for d in differences
        )
        graphic = any(
            (d.old_element or "").startswith(("G", "U"))
            or (d.new_element or "").startswith(("G", "U"))
            for d in differences
        )
        if kind != "ocr" and missing:
            kind = "correspondence"
        elif graphic:
            kind = "graphic"
        priority = {"ocr": 0, "correspondence": 1, "table": 2, "graphic": 3, "number": 4}.get(
            kind, 5
        )
        ar = ([] if graphic else element_regions(a)) or list(
            dict.fromkeys(r for d in differences for r in d.old_regions)
        )
        br = ([] if graphic else element_regions(b)) or list(
            dict.fromkeys(r for d in differences for r in d.new_regions)
        )
        units = _make_units("unverified", kind, a, b, context_chars, [root], ar, br, region_limit)
        ranked.append((priority, root, units))
    return [
        unit
        for _, _, units in sorted(ranked, key=lambda entry: (entry[0], entry[1]))
        for unit in units
    ]
