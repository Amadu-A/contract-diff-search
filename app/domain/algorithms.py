# app/domain/algorithms.py
"""Ограниченные алгоритмы выравнивания для разных уровней структуры.

Myers применяется к локальным токенам, Patience/Histogram — к опорам,
Rabin–Karp — к перемещениям, Smith–Waterman — только к коротким кандидатам.
"""

from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Hashable, Sequence


def myers(a: Sequence, b: Sequence, max_distance: int = 1500) -> list[tuple[str, int, int]]:
    """Возвращает кратчайшие equal/delete/insert; ограничивает размер следа поиска."""
    n, m = len(a), len(b)
    frontier, trace = {1: 0}, []
    for distance in range(min(n + m, max_distance) + 1):
        trace.append(frontier.copy())
        for diagonal in range(-distance, distance + 1, 2):
            if diagonal == -distance or (
                diagonal != distance
                and frontier.get(diagonal - 1, -1) < frontier.get(diagonal + 1, -1)
            ):
                x = frontier.get(diagonal + 1, 0)
            else:
                x = frontier.get(diagonal - 1, 0) + 1
            y = x - diagonal
            while x < n and y < m and a[x] == b[y]:
                x, y = x + 1, y + 1
            frontier[diagonal] = x
            if x >= n and y >= m:
                return _backtrack(trace, n, m)
    # Честная грубая замена вместо неограниченного роста памяти.
    return [("delete", i, 0) for i in range(n)] + [("insert", n, j) for j in range(m)]


def _backtrack(trace: list[dict[int, int]], x: int, y: int) -> list[tuple[str, int, int]]:
    """Восстанавливает минимальную последовательность правок по диагоналям Myers."""
    result = []
    for d in range(len(trace) - 1, -1, -1):
        v, k = trace[d], x - y
        previous = k + 1 if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)) else k - 1
        px = v.get(previous, 0)
        py = px - previous
        while x > px and y > py:
            x, y = x - 1, y - 1
            result.append(("equal", x, y))
        if d:
            if x == px:
                y -= 1
                result.append(("insert", x, y))
            else:
                x -= 1
                result.append(("delete", x, y))
    return list(reversed(result))


def increasing_pairs(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Находит монотонную цепь опор через LIS за O(n log n)."""
    if not pairs:
        return []
    pairs = sorted(pairs)
    tails, indices, previous = [], [], [-1] * len(pairs)
    for i, (_, value) in enumerate(pairs):
        place = bisect_left(tails, value)
        if place:
            previous[i] = indices[place - 1]
        if place == len(tails):
            tails.append(value)
            indices.append(i)
        else:
            tails[place], indices[place] = value, i
    cursor, result = indices[-1], []
    while cursor >= 0:
        result.append(pairs[cursor])
        cursor = previous[cursor]
    return result[::-1]


def patience(a: Sequence[Hashable], b: Sequence[Hashable]) -> list[tuple[int, int]]:
    """Находит уникальные общие опоры, устойчивые к вставкам больших разделов."""
    ca, cb = Counter(a), Counter(b)
    positions = {v: i for i, v in enumerate(b) if cb[v] == 1}
    return increasing_pairs(
        [(i, positions[v]) for i, v in enumerate(a) if ca[v] == 1 and v in positions]
    )


def histogram(
    a: Sequence[Hashable], b: Sequence[Hashable], rarity: int = 4
) -> list[tuple[int, int]]:
    """Использует редкие повторяющиеся элементы там, где уникальных опор мало."""
    ca, cb, positions = Counter(a), Counter(b), defaultdict(list)
    for j, value in enumerate(b):
        if cb[value] <= rarity:
            positions[value].append(j)
    pairs = [(i, j) for i, v in enumerate(a) if ca[v] <= rarity for j in positions[v]]
    # Для одного исходного индекса обратный порядок исключает повторное включение.
    pairs.sort(key=lambda p: (p[0], -p[1]))
    tails, chains = [], []
    for pair in pairs:
        pos = bisect_left(tails, pair[1])
        chain = (chains[pos - 1] if pos else []) + [pair]
        if pos == len(tails):
            tails.append(pair[1])
            chains.append(chain)
        else:
            tails[pos], chains[pos] = pair[1], chain
    return chains[-1] if chains else []


def lcs(a: Sequence, b: Sequence) -> list[tuple[int, int]]:
    """Извлекает LCS из ограниченного локального Myers без квадратичной матрицы."""
    return [(i, j) for action, i, j in myers(a, b) if action == "equal"]


def rolling_hash(values: Sequence[str], size: int = 4) -> dict[int, list[int]]:
    """Индексирует окна Rabin–Karp; потребитель обязан сверить содержимое при коллизии."""
    if len(values) < size:
        return {}
    modulus, base = 2**61 - 1, 1000003
    codes = [sum((i + 1) * ord(c) for i, c in enumerate(v)) % modulus for v in values]
    power = pow(base, size - 1, modulus)
    current = 0
    for value in codes[:size]:
        current = (current * base + value) % modulus
    result = defaultdict(list)
    result[current].append(0)
    for i in range(size, len(codes)):
        current = ((current - codes[i - size] * power) * base + codes[i]) % modulus
        result[current].append(i - size + 1)
    return dict(result)


def smith_waterman(a: Sequence, b: Sequence, limit: int = 256) -> float:
    """Оценивает длинное локальное сходство коротких кандидатов с линейной памятью."""
    a, b = a[:limit], b[:limit]
    previous, best = [0] * (len(b) + 1), 0
    for value in a:
        row = [0]
        for j, other in enumerate(b, 1):
            score = max(
                0, previous[j - 1] + (2 if value == other else -2), previous[j] - 1, row[-1] - 1
            )
            row.append(score)
            best = max(best, score)
        previous = row
    return best / max(1, 2 * max(len(a), len(b)))


def damerau_levenshtein(a: str, b: str) -> int:
    """Считает OSA-расстояние коротких строк, включая соседнюю перестановку символов."""
    if max(len(a), len(b)) > 512:
        raise ValueError("Символьное расстояние ограничено 512 символами")
    before, previous = None, list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        row = [i]
        for j, other in enumerate(b, 1):
            value = min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (char != other))
            if before is not None and j > 1 and char == b[j - 2] and a[i - 2] == other:
                value = min(value, before[j - 2] + 1)
            row.append(value)
        before, previous = previous, row
    return previous[-1]


def assignment(weights: list[list[float]], minimum: float = 0.4) -> list[tuple[int, int]]:
    """Венгерское паросочетание с фиктивными узлами: слабые пары не навязываются."""
    if not weights or not weights[0]:
        return []
    n, original_m = len(weights), len(weights[0])
    m = original_m + n
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        mins, used = [float("inf")] * (m + 1), [False] * (m + 1)
        while True:
            used[j0], i0, delta, j1 = True, p[j0], float("inf"), 0
            for j in range(1, m + 1):
                if not used[j]:
                    weight = weights[i0 - 1][j - 1] if j <= original_m else minimum
                    current = -weight - u[i0] - v[j]
                    if current < mins[j]:
                        mins[j], way[j] = current, j0
                    if mins[j] < delta:
                        delta, j1 = mins[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    mins[j] -= delta
            j0 = j1
            if not p[j0]:
                break
        while True:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
            if j0 == 0:
                break
    return [
        (p[j] - 1, j - 1)
        for j in range(1, original_m + 1)
        if p[j] and weights[p[j] - 1][j - 1] > minimum
    ]
