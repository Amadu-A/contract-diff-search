# app/domain/matching.py
"""Многоступенчатое сопоставление узлов с восстановлением после вставок.

Номер пункта помогает выбрать кандидата, но не заменяет сравнение содержания.
При близких конкурирующих кандидатах возвращается неоднозначность.
"""

import re
from collections import Counter, defaultdict

from app.domain.algorithms import (
    assignment,
    damerau_levenshtein,
    histogram,
    increasing_pairs,
    patience,
    rolling_hash,
    smith_waterman,
)
from app.domain.models import DocumentElement, ElementMatch
from app.domain.text import content, normalize, shingles, similarity, tokens


def match_elements(
    old: list[DocumentElement], new: list[DocumentElement]
) -> tuple[list[ElementMatch], set[str], set[str]]:
    """Сопоставляет структуру, перемещения и дробление; возвращает спорные идентификаторы."""
    a = [content(e.text) for e in old]
    b = [content(e.text) for e in new]
    used_a, used_b, matches = set(), set(), []
    ambiguous_a, ambiguous_b = set(), set()

    def add(i: int, j: int, method: str, score: float = 1.0) -> None:
        """Фиксирует однозначную пару без повторного использования узла."""
        if i not in used_a and j not in used_b:
            matches.append(ElementMatch([old[i].id], [new[j].id], method, score))
            used_a.add(i)
            used_b.add(j)

    anchors = patience(a, b)
    for i, j in anchors:
        add(i, j, "patience")
    # Histogram работает внутри опорных окон: одинаковая фраза из другого раздела не подходит.
    borders = [(-1, -1), *anchors, (len(a), len(b))]
    for (ai, bi), (aj, bj) in zip(borders, borders[1:], strict=False):
        for i, j in histogram(a[ai + 1 : aj], b[bi + 1 : bj]):
            if old[ai + 1 + i].section == new[bi + 1 + j].section:
                add(ai + 1 + i, bi + 1 + j, "histogram")

    # Поиск точных перенесённых блоков через rolling hash с обязательной проверкой текста.
    index = rolling_hash(b, 1)
    for i, value in enumerate(a):
        if i in used_a or len(value) < 12:
            continue
        hashes = rolling_hash([value], 1)
        candidates = [
            j for j in index.get(next(iter(hashes)), []) if j not in used_b and b[j] == value
        ]
        if len(candidates) == 1:
            add(i, candidates[0], "rabin_karp")

    # Сначала содержательные split/merge, затем частично изменённые кандидаты.
    for left, right, occupied_l, occupied_r, reverse in (
        (old, new, used_a, used_b, False),
        (new, old, used_b, used_a, True),
    ):
        grouped = defaultdict(list)
        for j in range(len(right)):
            for count in (2, 3):
                group = right[j : j + count]
                if len(group) == count and not any(j + k in occupied_r for k in range(count)):
                    combined = normalize(" ".join(content(e.text) for e in group)).text
                    grouped[combined].append((j, count))
        for i, element in enumerate(left):
            if i in occupied_l or len(element.text) < 30:
                continue
            expected = normalize(content(element.text)).text
            candidates = [
                (j, count)
                for j, count in grouped.get(expected, [])
                if not any(j + k in occupied_r for k in range(count))
            ]
            if len(candidates) == 1:
                j, count = candidates[0]
                l_ids, r_ids = [element.id], [e.id for e in right[j : j + count]]
                matches.append(
                    ElementMatch(
                        r_ids if reverse else l_ids,
                        l_ids if reverse else r_ids,
                        "tree_split_merge",
                        1.0,
                    )
                )
                occupied_l.add(i)
                occupied_r.update(range(j, j + count))

    sections, grams = defaultdict(list), defaultdict(set)
    for j, element in enumerate(new):
        if j not in used_b:
            sections[element.section].append(j)
            for gram in shingles(element.text):
                grams[gram].add(j)
    pending = []
    for i, element in enumerate(old):
        if i in used_a:
            continue
        votes = Counter(
            j for gram in shingles(element.text) if len(grams[gram]) <= 128 for j in grams[gram]
        )
        candidates = set(j for j, _ in votes.most_common(24))
        if element.section:
            candidates.update(sections[element.section][:24])
        scores = []
        for j in candidates:
            if j in used_b:
                continue
            other = new[j]
            if (
                element.kind == "cell"
                and "column" in element.attributes
                and "column" in other.attributes
                and element.attributes["column"] != other.attributes["column"]
            ):
                continue
            score = similarity(element.text, other.text)
            same_section = bool(element.section and element.section == other.section)
            if same_section and 3 <= max(len(a[i]), len(b[j])) <= 64:
                distance = damerau_levenshtein(a[i], b[j])
                if distance <= 2:
                    score = max(score, 1 - distance / max(len(a[i]), len(b[j])))
            if score < 0.4 and same_section:
                score = max(
                    score,
                    smith_waterman([t[0] for t in tokens(a[i])], [t[0] for t in tokens(b[j])]),
                )
            if same_section:
                score += 0.18
            label_a = re.sub(r"\d+", "", element.text).strip().casefold()
            label_b = re.sub(r"\d+", "", other.text).strip().casefold()
            if label_a and label_a == label_b and re.search(r"\d{8,}", element.text + other.text):
                score += 0.3
            # Строки таблиц с устойчивым первым столбцом имеют приоритет.
            if element.attributes.get("row_key") and element.attributes.get(
                "row_key"
            ) == other.attributes.get("row_key"):
                score += 0.22
            if score >= 0.43:
                scores.append((score, j))
        scores.sort(reverse=True)
        if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.06:
            ambiguous_a.add(element.id)
            ambiguous_b.update(new[j].id for _, j in scores[:2])
        elif scores:
            pending.append((i, scores))

    # Ограниченные компоненты двудольного графа вместо общей квадратной матрицы.
    for offset in range(0, len(pending), 64):
        group = pending[offset : offset + 64]
        js = sorted({j for _, scores in group for _, j in scores if j not in used_b})
        weights = [[dict((j, s) for s, j in scores).get(j, 0) for j in js] for _, scores in group]
        for ii, jj in assignment(weights, 0.43):
            add(group[ii][0], js[jj], "tree_weighted_assignment", min(1, weights[ii][jj]))

    old_pos = {e.id: i for i, e in enumerate(old)}
    new_pos = {e.id: i for i, e in enumerate(new)}
    pairs = [(old_pos[m.old[0]], new_pos[m.new[0]]) for m in matches]
    ordered = set(increasing_pairs(pairs))
    for match, pair in zip(matches, pairs, strict=False):
        match.moved = pair not in ordered
    return matches, ambiguous_a, ambiguous_b
