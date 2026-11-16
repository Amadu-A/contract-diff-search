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
from app.domain.text import (
    NUMBER,
    EquivalenceGroups,
    comparison_content,
    content,
    excluded_pair,
    exclusion_value,
    ocr_equivalent,
    shingles,
    similarity,
    tokens,
)


def _overlap(left: str, right: str) -> float:
    """Оценивает общий словарь кандидатов, не объявляя разные числа эквивалентными."""
    a, b = {t[0].casefold() for t in tokens(left)}, {t[0].casefold() for t in tokens(right)}
    return len(a & b) / max(1, len(a | b))


def _field(text: str) -> str:
    """Узнаёт назначение реквизита, сохраняя его значение для последующего сравнения."""
    text = text.strip().casefold()
    for key, pattern in (
        ("correspondent", r"^(?:кор[.]?\s*сч[её]т|к/с(?:ч[её]т)?)"),
        ("settlement", r"^р/с"),
        ("address", r"^адрес:"),
        ("inn", r"^инн"),
        ("kpp", r"^кпп"),
        ("ogrn", r"^огрн"),
        ("bik", r"^бик"),
        ("email", r"^e-mail"),
    ):
        if re.match(pattern, text):
            return key
    return ""


def match_elements(
    old: list[DocumentElement],
    new: list[DocumentElement],
    exclusions: EquivalenceGroups = (),
    ocr_exclusions: EquivalenceGroups = (),
) -> tuple[list[ElementMatch], set[str], set[str]]:
    """Сопоставляет структуру, перемещения и дробление; возвращает спорные идентификаторы."""
    a = [comparison_content(e.text, exclusions) for e in old]
    b = [comparison_content(e.text, exclusions) for e in new]
    used_a, used_b, matches = set(), set(), []
    ambiguous_a, ambiguous_b = set(), set()

    def add(i: int, j: int, method: str, score: float = 1.0) -> None:
        """Фиксирует однозначную пару без повторного использования узла."""
        if i not in used_a and j not in used_b:
            matches.append(ElementMatch([old[i].id], [new[j].id], method, score))
            used_a.add(i)
            used_b.add(j)

    # Повторяющиеся целые значения группы не служат содержательными опорами между разделами.
    excluded_a = {i for i, e in enumerate(old) if exclusion_value(content(e.text), exclusions)}
    excluded_b = {j for j, e in enumerate(new) if exclusion_value(content(e.text), exclusions)}

    def compatible(i, j):
        """Не переносит одинаковый номер/слово между разными пунктами или сторонами таблицы."""
        left, right = old[i], new[j]
        if bool(left.section) != bool(right.section):
            return False
        if (
            "cell" in {left.kind, right.kind}
            and "column" in left.attributes
            and "column" in right.attributes
            and left.attributes["column"] != right.attributes["column"]
        ):
            return False
        if _field(left.text) and _field(right.text) and _field(left.text) != _field(right.text):
            return False
        return len(tokens(a[i])) >= 5 or left.section == right.section

    anchors = [
        (i, j)
        for i, j in patience(a, b)
        if i not in excluded_a and j not in excluded_b and compatible(i, j)
    ]

    def exclusion_pair(i, j):
        """Разрешает пару только в одном пункте/ячейке, не перенося тире между разделами."""
        left, right = old[i], new[j]
        if left.section != right.section or left.kind != right.kind:
            return
        before, after = content(left.text), content(right.text)
        if not excluded_pair(before, after, (0, len(before)), (0, len(after)), exclusions):
            return
        if any(
            comparison_content(left.attributes.get(k, ""), exclusions)
            != comparison_content(right.attributes.get(k, ""), exclusions)
            for k in ("column", "row_key")
        ):
            return
        add(i, j, "comparison_exclusion")

    # Уникальная структурная позиция важнее самого повторяющегося символа.
    indexed_a, indexed_b = defaultdict(list), defaultdict(list)
    for elements, indices, indexed in ((old, excluded_a, indexed_a), (new, excluded_b, indexed_b)):
        for index in indices:
            element = elements[index]
            if element.section or element.attributes.get("row_key"):
                indexed[
                    (
                        element.section,
                        element.kind,
                        comparison_content(element.attributes.get("row_key", ""), exclusions),
                        element.attributes.get("column", ""),
                    )
                ].append(index)
    for key in indexed_a.keys() & indexed_b.keys():
        if len(indexed_a[key]) == len(indexed_b[key]) == 1:
            exclusion_pair(indexed_a[key][0], indexed_b[key][0])
    # Между теми же опорами безопасна единственная пара целых значений.
    borders = [(-1, -1), *anchors, (len(a), len(b))]
    for (ai, bi), (aj, bj) in zip(borders, borders[1:], strict=False):
        if aj - ai == bj - bi == 2 and ai + 1 in excluded_a and bi + 1 in excluded_b:
            exclusion_pair(ai + 1, bi + 1)
    for i, j in anchors:
        add(i, j, "patience")
    # Histogram работает внутри опорных окон: одинаковая фраза из другого раздела не подходит.
    borders = [(-1, -1), *anchors, (len(a), len(b))]
    for (ai, bi), (aj, bj) in zip(borders, borders[1:], strict=False):
        for i, j in histogram(a[ai + 1 : aj], b[bi + 1 : bj]):
            oi = ai + 1 + i
            nj = bi + 1 + j
            choices = [
                k
                for k in range(bi + 1, bj)
                if b[k] == a[oi] and new[k].section == old[oi].section and compatible(oi, k)
            ]
            reverse = [
                k
                for k in range(ai + 1, aj)
                if a[k] == b[nj] and old[k].section == new[nj].section and compatible(k, nj)
            ]
            if len(choices) > 1 or len(reverse) > 1:
                ambiguous_a.update(old[k].id for k in reverse)
                ambiguous_b.update(new[k].id for k in choices)
                continue
            if (
                ai + 1 + i not in excluded_a
                and bi + 1 + j not in excluded_b
                and old[ai + 1 + i].section == new[bi + 1 + j].section
                and compatible(ai + 1 + i, bi + 1 + j)
            ):
                add(ai + 1 + i, bi + 1 + j, "histogram")

    # Поиск точных перенесённых блоков через rolling hash с обязательной проверкой текста.
    index = rolling_hash(b, 1)
    old_index = rolling_hash(a, 1)
    for i, value in enumerate(a):
        if i in used_a or i in excluded_a or not value:
            continue
        hashes = rolling_hash([value], 1)
        candidates = [
            j
            for j in index.get(next(iter(hashes)), [])
            if j not in used_b and j not in excluded_b and b[j] == value and compatible(i, j)
        ]
        if len(candidates) == 1:
            j = candidates[0]
            reverse = [
                k
                for k in old_index[next(iter(hashes))]
                if k not in used_a and a[k] == value and compatible(k, j)
            ]
            if len(reverse) > 1:
                ambiguous_a.update(old[k].id for k in reverse)
                ambiguous_b.add(new[j].id)
            else:
                add(i, j, "rabin_karp")

    # Сначала содержательные split/merge, затем частично изменённые кандидаты.
    for left, right, occupied_l, occupied_r, reverse in (
        (old, new, used_a, used_b, False),
        (new, old, used_b, used_a, True),
    ):
        bounds = [(y, x) if reverse else (x, y) for x, y in anchors]
        windows = [(-1, -1), *bounds, (len(left), len(right))]
        scope = {
            i: (start + 1, end)
            for (lo, start), (hi, end) in zip(windows, windows[1:], strict=False)
            for i in range(lo + 1, hi)
        }
        grouped = defaultdict(list)
        for j in range(len(right)):
            for count in range(2, 13):
                group = right[j : j + count]
                if (
                    len(group) == count
                    and len({e.section for e in group}) == 1
                    and not any(j + k in occupied_r for k in range(count))
                ):
                    combined = comparison_content(
                        " ".join(content(e.text) for e in group), exclusions
                    )
                    grouped[combined].append((j, count))
        for i, element in enumerate(left):
            if i in occupied_l or len(element.text) < 30:
                continue
            expected = comparison_content(element.text, exclusions)
            candidates = [
                (j, count)
                for j, count in grouped.get(expected, [])
                if not any(j + k in occupied_r for k in range(count))
                and scope[i][0] <= j
                and j + count <= scope[i][1]
                and bool(element.section) == bool(right[j].section)
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

        # Общий абзац может быть хвостом изменённого блока, в том числе в преамбуле.
        # Ищем группу в том же опорном окне, а не одинаковую фразу в другом разделе.
        keys_l, keys_r = (b, a) if reverse else (a, b)
        bounds = [(y, x) if reverse else (x, y) for x, y in anchors]
        windows = [(-1, -1), *bounds, (len(left), len(right))]
        token_l = [tuple(t[0] for t in tokens(value)) for value in keys_l]
        token_r = [tuple(t[0] for t in tokens(value)) for value in keys_r]
        for (lo, start), (hi, end) in zip(windows, windows[1:], strict=False):
            for i in range(lo + 1, hi):
                element = left[i]
                if i in occupied_l or len(token_l[i]) < 8:
                    continue
                expected = " " + " ".join(token_l[i]) + " "
                contained = [
                    j
                    for j in range(start + 1, end)
                    if j not in occupied_r
                    and right[j].section == element.section
                    and len(token_r[j]) >= 5
                    and expected.count(" " + " ".join(token_r[j]) + " ") == 1
                ]
                candidates = {}
                target = Counter(token_l[i])
                for pivot in contained:
                    for j in range(max(start + 1, pivot - 11), pivot + 1):
                        group_tokens = []
                        for stop in range(j, min(end, j + 12)):
                            other = right[stop]
                            if (
                                stop in occupied_r
                                or other.section != element.section
                                or (
                                    "cell" in {element.kind, other.kind}
                                    and "column" in element.attributes
                                    and "column" in other.attributes
                                    and element.attributes["column"] != other.attributes["column"]
                                )
                            ):
                                break
                            group_tokens.extend(token_r[stop])
                            if stop < pivot or stop == j:
                                continue
                            score = (
                                2
                                * sum((target & Counter(group_tokens)).values())
                                / max(1, len(token_l[i]) + len(group_tokens))
                            )
                            single = max(
                                2
                                * sum((target & Counter(token_r[k])).values())
                                / max(1, len(token_l[i]) + len(token_r[k]))
                                for k in range(j, stop + 1)
                            )
                            if score >= 0.75 and score >= single + 0.1:
                                candidates[j, stop + 1] = score
                ranked = sorted(
                    ((score, j, stop) for (j, stop), score in candidates.items()), reverse=True
                )
                if not ranked:
                    continue
                competing = [
                    candidate
                    for candidate in ranked[1:]
                    if not (
                        ranked[0][1] <= candidate[1]
                        and ranked[0][2] >= candidate[2]
                        or candidate[1] <= ranked[0][1]
                        and candidate[2] >= ranked[0][2]
                    )
                ]
                if competing and ranked[0][0] - competing[0][0] < 0.06:
                    own_ambiguous, other_ambiguous = (
                        (ambiguous_b, ambiguous_a) if reverse else (ambiguous_a, ambiguous_b)
                    )
                    own_ambiguous.add(element.id)
                    other_ambiguous.update(
                        right[k].id
                        for _, j, stop in (ranked[0], competing[0])
                        for k in range(j, stop)
                    )
                    continue
                score, j, stop = ranked[0]
                l_ids, r_ids = [element.id], [e.id for e in right[j:stop]]
                matches.append(
                    ElementMatch(
                        r_ids if reverse else l_ids,
                        l_ids if reverse else r_ids,
                        "context_split_merge",
                        score,
                    )
                )
                occupied_l.add(i)
                occupied_r.update(range(j, stop))

    # Изменённый пункт тоже может быть разделён на несколько абзацев только с одной стороны.
    for left, right, occupied_l, occupied_r, reverse in (
        (old, new, used_a, used_b, False),
        (new, old, used_b, used_a, True),
    ):
        field_index = defaultdict(list)
        for j, other in enumerate(right):
            field_index[(other.section, other.attributes.get("column"), _field(other.text))].append(
                j
            )
        for i, element in enumerate(left):
            if i in occupied_l or not _field(element.text):
                continue
            candidates = []
            for j in field_index[
                (element.section, element.attributes.get("column"), _field(element.text))
            ]:
                other = right[j]
                if (
                    j in occupied_r
                    or _field(other.text) != _field(element.text)
                    or other.section != element.section
                    or other.attributes.get("column") != element.attributes.get("column")
                ):
                    continue
                for count in range(2, 5):
                    group = right[j : j + count]
                    if len(group) != count or any(
                        j + k in occupied_r
                        or e.section != element.section
                        or e.attributes.get("column") != element.attributes.get("column")
                        for k, e in enumerate(group)
                    ):
                        break
                    if any(
                        _field(e.text)
                        and _field(e.text) != _field(element.text)
                        and not (
                            _field(element.text) == "inn"
                            and _field(e.text) == "kpp"
                            and "кпп" in element.text.casefold()
                        )
                        for e in group[1:]
                    ):
                        break
                    joined = " ".join(e.text for e in group)
                    score = _overlap(element.text, joined)
                    if score >= 0.65 and score > _overlap(element.text, other.text) + 0.1:
                        candidates.append((score, j, count))
            candidates.sort(reverse=True)
            if candidates and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 0.06):
                score, j, count = candidates[0]
                l_ids, r_ids = [element.id], [e.id for e in right[j : j + count]]
                matches.append(
                    ElementMatch(
                        r_ids if reverse else l_ids,
                        l_ids if reverse else r_ids,
                        "field_split_merge",
                        score,
                    )
                )
                occupied_l.add(i)
                occupied_r.update(range(j, j + count))
    for section in {e.section for e in old} & {e.section for e in new} - {""}:
        left = [i for i, e in enumerate(old) if e.section == section and i not in used_a]
        right = [j for j, e in enumerate(new) if e.section == section and j not in used_b]
        if (
            not left
            or not right
            or max(len(left), len(right)) > 12
            or min(len(left), len(right)) != 1
        ):
            continue
        if any(
            sum(bool(NUMBER.match(elements[index].text.lstrip())) for index in indices) > 1
            for elements, indices in ((old, left), (new, right))
        ):
            continue
        before = " ".join(a[i] for i in left)
        after = " ".join(b[j] for j in right)
        if max(similarity(before.casefold(), after.casefold()), _overlap(before, after)) < 0.4:
            continue
        if len(left) > 1 or len(right) > 1:
            matches.append(
                ElementMatch(
                    [old[i].id for i in left],
                    [new[j].id for j in right],
                    "section_split_merge",
                    0.85,
                )
            )
            used_a.update(left)
            used_b.update(right)

    sections, grams = defaultdict(list), defaultdict(set)
    for j, element in enumerate(new):
        if j not in used_b and j not in excluded_b:
            sections[element.section].append(j)
            for gram in shingles(b[j]):
                grams[gram].add(j)
    pending = []
    for i, element in enumerate(old):
        if i in used_a or i in excluded_a or element.id in ambiguous_a:
            continue
        votes = Counter(
            j for gram in shingles(a[i]) if len(grams[gram]) <= 128 for j in grams[gram]
        )
        candidates = set(j for j, _ in votes.most_common(24))
        if element.section:
            candidates.update(sections[element.section][:24])
        scores = []
        for j in candidates:
            if j in used_b:
                continue
            other = new[j]
            if a[i] == b[j] and not compatible(i, j):
                continue
            if (
                _field(element.text)
                and _field(other.text)
                and _field(element.text) != _field(other.text)
            ):
                continue
            if (
                element.kind == "cell"
                and "column" in element.attributes
                and "column" in other.attributes
                and element.attributes["column"] != other.attributes["column"]
            ):
                continue
            score = similarity(a[i].casefold(), b[j].casefold())
            same_section = bool(element.section and element.section == other.section)
            if same_section and _field(element.text) and _field(element.text) == _field(other.text):
                score = max(score, 0.7)
            if same_section:
                score = max(score, _overlap(a[i], b[j]))
            if same_section and ocr_equivalent(
                content(element.text),
                content(other.text),
                " ".join(e.text for e in old[max(0, i - 1) : i + 2]),
                " ".join(e.text for e in new[max(0, j - 1) : j + 2]),
                ocr_exclusions,
            ):
                score = max(score, 0.9)
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
            label_a = re.sub(r"\d+", "", a[i]).strip().casefold()
            label_b = re.sub(r"\d+", "", b[j]).strip().casefold()
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
