# app/application/golden.py
"""Независимая сверка результата с локальной вручную проверенной матрицей.

Эталон не изменяется. Неисчерпывающая матрица не используется для заявления
абсолютной precision; лишние события возвращаются на отдельный ручной аудит.
"""

import re
from collections import Counter


def compact(value: str | None) -> str:
    """Убирает только пробелы для записи процентов; цифры и буквы не исправляет."""
    return re.sub(r"\s+", "", value or "")


def expected_tokens(text: str) -> Counter:
    """Считает ожидаемые слова/числа независимо от алгоритма формирования diff."""
    return Counter(re.findall(r"\d+(?:[.,]\d+)*%?|[^\W\d_]+|[^\w\s]", text, re.UNICODE))


def overlaps(region: dict, box: list[float]) -> bool:
    """Проверяет пересечение локального маркера с независимо размеченной областью."""
    return min(region["x1"], box[2]) > max(region["x0"], box[0]) and min(
        region["y1"], box[3]
    ) > max(region["y0"], box[1])


def control_observed(result: dict, side: str, section: str, value: str) -> bool:
    """Не объявляет контроль совпавшим при пустом или неподтверждённом OCR."""
    pattern = r"\s*".join(re.escape(char) for char in compact(value))
    for element in result[side]["elements"]:
        if element["section"] != section:
            continue
        match = re.search(pattern, element["text"])
        if match is None:
            continue
        words = [
            word
            for word in element["words"]
            if word["end"] > match.start() and word["start"] < match.end()
        ]
        if (
            words
            and compact(value) in compact("".join(word["text"] for word in words))
            and all(word["confidence"] >= 0.65 and word["verified"] for word in words)
        ):
            return True
    return False


def validate_matrix(matrix: dict, result: dict) -> dict:
    """Сверяет значения, пункт, обе геометрии и отрицательные контроли отдельно."""
    rows, accounted = [], set()
    differences = [d for d in result["differences"] if d.get("active", True)]
    for entry in matrix["entries"]:
        expected = entry["type"]
        candidates = [
            d
            for d in differences
            if (
                not entry.get("old_section")
                or d["old_section"] == entry["old_section"]
                or d["new_section"] == entry["new_section"]
            )
        ]
        found = []
        if expected == "unchanged":
            bad = [
                d
                for d in candidates
                if d["category"] in {"number_modified", "numbering_changed"}
                and compact(d["old_text"]) == compact(entry["old_text"])
            ]
            state = "FAIL" if bad else "PASS"
            reason = (
                "Ложное изменение отрицательного контроля"
                if bad
                else "Контрольное значение не объявлено изменённым"
            )
            observed = control_observed(
                result, "old", entry["old_section"], entry["old_text"]
            ) and control_observed(result, "new", entry["new_section"], entry["new_text"])
            if not bad and not observed:
                state, reason = (
                    "UNVERIFIED",
                    "Контроль не изменён, но OCR/геометрия значения не подтверждены",
                )
        elif expected == "graphic_added":
            found = [
                d
                for d in differences
                if d["category"] == "graphic_added"
                and any(r["page"] == entry["new_page"] for r in d["new_regions"])
            ]
            state = "PASS" if len(found) >= 2 else "FAIL"
            reason = (
                "Две локальные области графики"
                if state == "PASS"
                else "Недостаточно найденных графических объектов"
            )
        elif (
            expected == "text_modified"
            and len(entry.get("old_text") or "") > 60
            and "requires_review" in entry.get("review_status", "")
        ):
            found = [
                d
                for d in candidates
                if d["old_regions"]
                and d["new_regions"]
                and d["category"] in {"text_modified", "number_modified"}
            ]
            state = "UNVERIFIED" if found else "FAIL"
            reason = (
                "Изменение адреса локализовано; точное написание всего адреса требует проверки OCR"
            )
        elif (
            expected == "text_modified"
            and len(entry.get("old_text") or "") > 25
            and entry.get("new_text")
        ):
            before, after = expected_tokens(entry["old_text"]), expected_tokens(entry["new_text"])
            required_old, required_new = before - after, after - before
            found = [
                d
                for d in candidates
                if d["category"]
                in {"text_modified", "number_modified", "text_added", "text_deleted"}
            ]
            observed_old = expected_tokens(" ".join(d["old_text"] for d in found))
            observed_new = expected_tokens(" ".join(d["new_text"] for d in found))
            values_match = all(
                observed_old[token] >= count for token, count in required_old.items()
            ) and all(observed_new[token] >= count for token, count in required_new.items())
            geometry = any(d["old_regions"] for d in found) and any(d["new_regions"] for d in found)
            state = (
                "PASS"
                if values_match and geometry and all(d["status"] == "confirmed" for d in found)
                else "UNVERIFIED"
                if values_match
                else "FAIL"
            )
            reason = (
                "Все изменённые слова фразы присутствуют в локальных событиях нужного пункта"
                if values_match
                else "Не все ожидаемые изменения фразы найдены"
            )
        else:
            found = [
                d
                for d in candidates
                if compact(d["old_text"]) == compact(entry.get("old_text"))
                and compact(d["new_text"]) == compact(entry.get("new_text"))
                and d["category"] == expected
            ]
            confirmed = [d for d in found if d["status"] == "confirmed"]
            if confirmed:
                found = confirmed
            if not found:
                state, reason = "FAIL", "Ожидаемые значения не найдены в нужном пункте"
            else:
                geometry = all(
                    (not entry.get("old_text") or d["old_regions"])
                    and (not entry.get("new_text") or d["new_regions"])
                    for d in found
                )
                geometry = geometry and all(
                    (
                        not entry.get("old_page")
                        or all(r["page"] == entry["old_page"] for r in d["old_regions"])
                    )
                    and (
                        not entry.get("new_page")
                        or all(r["page"] == entry["new_page"] for r in d["new_regions"])
                    )
                    for d in found
                )
                for side in ("old", "new"):
                    expected_boxes = entry.get(side + "_regions") or []
                    actual_regions = [r for d in found for r in d[side + "_regions"]]
                    if expected_boxes:
                        geometry = geometry and any(
                            overlaps(region, box)
                            for region in actual_regions
                            for box in expected_boxes
                        )
                reliable = all(d["status"] == "confirmed" for d in found)
                state = "PASS" if geometry and reliable else "UNVERIFIED"
                reason = (
                    "Значения, пункт и геометрия совпали"
                    if state == "PASS"
                    else "Значение найдено, но требуется проверка достоверности/геометрии"
                )
        accounted.update(d["id"] for d in found)
        rows.append(
            {
                "id": entry["id"],
                "expected_type": expected,
                "status": state,
                "reason": reason,
                "difference_ids": [d["id"] for d in found],
                "old_regions": [r for d in found for r in d["old_regions"]],
                "new_regions": [r for d in found for r in d["new_regions"]],
            }
        )
    return {
        "exhaustive": bool(matrix.get("exhaustive")),
        "rows": rows,
        "passed": sum(r["status"] == "PASS" for r in rows),
        "failed": sum(r["status"] == "FAIL" for r in rows),
        "unverified": sum(r["status"] == "UNVERIFIED" for r in rows),
        "unreviewed_difference_ids": [d["id"] for d in differences if d["id"] not in accounted],
        "precision": None,
        "precision_reason": "Матрица не исчерпывающая; лишние события не классифицированы вручную",
    }
