# tests/regression/test_ocr_recheck.py
"""Адресные доказательства коротких OCR-различий, контекстные омоглифы и сохранение реальных изменений."""

from dataclasses import asdict, replace

import pytest

from app.application.ai_evidence import merge_reply
from app.application.ai_planning import full_plan, unverified_plan
from app.bootstrap import load_exclusions
from app.domain.comparison import compare, resolve_ocr, statistics
from app.domain.models import AIObservation, AIReply
from app.domain.text import canonicalize
from tests.factories import version


def short_result(before="не", after="т"):
    """Создаёт однозначно сопоставленную пару с читаемыми соседями и OCR на стороне контрагента."""
    old = version([f"10.2. Платёж по причинам {before} зависит от сторон."])
    new = version([f"10.2. Платёж по причинам {after} зависит от сторон."], "pdf")
    new.pages[0].mode = "scanned"
    return compare(old, new)


@pytest.mark.parametrize(
    "reading,outcome",
    [("т", "confirmed_difference"), ("не", "confirmed_equal"), (None, "unverified")],
)
def test_short_difference_requires_local_proof_and_has_precise_reason(reading, outcome):
    """Разные прочитанные значения подтверждаются, совпадение снимается, нечитаемый растр остаётся непроверенным."""
    result = short_result()
    difference = next(d for d in result.differences if d.active)
    assert difference.unverified_reason == "ocr_uncertain"
    documents = asdict(result.old), asdict(result.new)
    count = len(result.differences)
    assert (
        resolve_ocr(result, difference, "не", reading, confidence=0.95, error="unreadable_image")
        == outcome
    )
    assert len(result.differences) == count
    assert (asdict(result.old), asdict(result.new)) == documents
    if outcome == "confirmed_difference":
        assert difference.status == "confirmed" and difference.active
        assert statistics(result.differences)["total"] == 1
        assert statistics(result.differences)["unverified"] == 0
        assert difference.verified_by == "ocr_recheck"
        assert difference.old_regions and difference.new_regions
    elif outcome == "confirmed_equal":
        assert not difference.active
        assert (
            statistics(result.differences)["total"]
            == statistics(result.differences)["unverified"]
            == 0
        )
        assert result.coverage.old == result.coverage.new == {"E000000": "unchanged"}
        assert result.coverage.resolved_algorithms == 1
        assert unverified_plan(result, 3600) == []
    else:
        assert difference.status == "unverified" and difference.active
        assert difference.unverified_reason == "unreadable_image"


def test_same_short_text_is_not_a_difference():
    """Одинаковое достоверное «не» не направляется на повторную проверку."""
    assert short_result("не", "не").differences == []


@pytest.mark.parametrize(
    "before,after",
    [
        ("ОКС", "OKC"),
        ("М", "M"),
        ("В", "B"),
        ("А", "A"),
        ("Е", "E"),
        ("Н", "H"),
        ("Р", "P"),
        ("Т", "T"),
        ("Х", "X"),
        ("сосна", "cocна"),
    ],
)
def test_ocr_homoglyph_groups_need_russian_context_and_stable_reading(settings, before, after):
    """Сопоставленный русский текст допускает настроенные омоглифы; исходные Unicode-значения сохраняются."""
    old = version([f"1.2. Условия для {before} указаны в договоре."])
    new = version([f"1.2. Условия для {after} указаны в договоре."], "pdf")
    new.pages[0].mode = "scanned"
    original = asdict(old), asdict(new)
    result = compare(old, new, ocr_exclusions=load_exclusions(settings, "ocr"))
    assert result.differences == []
    assert (asdict(old), asdict(new)) == original
    assert canonicalize(after, load_exclusions(settings)).text == after
    new.pages[0].mode = "text"
    assert compare(old, new, ocr_exclusions=load_exclusions(settings, "ocr")).differences


@pytest.mark.parametrize(
    "label,before,after",
    [
        ("Артикул", "ОКС123", "OKC123"),
        ("Код", "М", "M"),
        ("Модель", "В", "B"),
        ("Счёт", "РО123", "PO123"),
    ],
)
def test_identifier_alphabets_are_not_automatically_equivalent(settings, label, before, after):
    """Артикулы, модели и реквизиты не исчезают после настройки OCR-групп."""
    old = version([f"1.2. {label} {before} указан в данном договоре."])
    new = version([f"1.2. {label} {after} указан в данном договоре."], "pdf")
    new.pages[0].mode = "scanned"
    result = compare(old, new, ocr_exclusions=load_exclusions(settings, "ocr"))
    assert any(d.active for d in result.differences)


def test_uncertain_homoglyph_word_still_requires_recheck(settings):
    """Низкая уверенность в похожем слове не превращается в автоматическое равенство."""
    old, new = (
        version(["1.2. Условия для ОКС указаны в договоре."]),
        version(["1.2. Условия для OKC указаны в договоре."], "pdf"),
    )
    new.pages[0].mode = "scanned"
    new.elements[0].words = [
        replace(w, verified=False, confidence=0.4) if w.text == "OKC" else w
        for w in new.elements[0].words
    ]
    result = compare(old, new, ocr_exclusions=load_exclusions(settings, "ocr"))
    assert any(d.active and d.unverified_reason == "ocr_uncertain" for d in result.differences)


def test_another_scanned_page_does_not_change_native_text_rules(settings):
    """OCR на второй странице не подавляет различия нативных алфавитов первой страницы."""
    old = version(["1.2. Условия для ОКС указаны в договоре.", "2.1. Второй пункт без изменений."])
    new = version(
        ["1.2. Условия для OKC указаны в договоре.", "2.1. Второй пункт без изменений."], "pdf"
    )
    new.pages[1].mode = "scanned"
    result = compare(old, new, ocr_exclusions=load_exclusions(settings, "ocr"))
    assert any(d.active and d.old_section == "1.2" for d in result.differences)


def test_real_numbers_and_negation_are_preserved_with_ocr_groups(settings):
    """Настройка букв не скрывает проценты или удаление отрицания."""
    for before, after in [("70%", "50%"), ("не вправе", "вправе")]:
        result = short_result(before, after)
        assert result.differences
        assert any(d.active for d in result.differences)


def test_ocr_groups_have_their_own_validated_scope(settings, tmp_path):
    """Парсер разделяет группы и сообщает строку ошибки вместо глобальной нормализации алфавитов."""
    path = tmp_path / "groups.txt"
    settings.comparison_exclusions_file = path
    path.write_text("\ufeff[-, —, –]\n\n ocr: [О, O] \nocr: [М, M]\n", encoding="utf-8")
    assert load_exclusions(settings) == (("-", "—", "–"),)
    assert load_exclusions(settings, "ocr") == (("О", "O"), ("М", "M"))
    for invalid in ("ocr: [ОК, OK]", "ocr: [О, 0]", "other: [О, O]", "ocr: [О, O]\nocr: [O, C]"):
        path.write_text(invalid, encoding="utf-8")
        with pytest.raises(ValueError, match="строка"):
            load_exclusions(settings)


def test_full_vlm_does_not_restore_a_contextually_resolved_ocr_alias(settings):
    """Дополнительный полный проход не создаёт заново замечание только об алфавите русского слова."""
    old, new = (
        version(["1.2. Условия для ОКС указаны в договоре."]),
        version(["1.2. Условия для OKC указаны в договоре."], "pdf"),
    )
    new.pages[0].mode = "scanned"
    result = compare(old, new, ocr_exclusions=load_exclusions(settings, "ocr"))
    unit = next(
        u
        for u in full_plan(result, 3600)
        if u.old_elements and u.new_elements and u.kind != "correspondence"
    )
    merge_reply(
        result,
        unit,
        AIReply(
            "different",
            True,
            [
                AIObservation(
                    "text_modified",
                    "ОКС",
                    "OKC",
                    "o0",
                    "n0",
                    old_quote="для ОКС",
                    new_quote="для OKC",
                )
            ],
        ),
    )
    assert result.differences == []


def test_unavailable_crop_recheck_preserves_primary_result(settings):
    """Отсутствующий локальный растр не уничтожает базовый результат и не подтверждает OCR."""
    from app.infrastructure.ocr import TesseractOCR
    from app.infrastructure.pdf import PdfDocumentExtractor
    from app.infrastructure.word import LibreOffice

    result = short_result()
    PdfDocumentExtractor(settings, TesseractOCR(settings), LibreOffice(settings)).recheck(
        result, lambda stage: None
    )
    assert any(d.active and d.status == "unverified" for d in result.differences)


def test_fragment_ocr_uses_two_segmentation_modes_and_preserves_image(settings, tmp_path):
    """OCR-контекст перечитывается разными сегментаторами; входной растр не переписывается."""
    from PIL import Image

    from app.infrastructure.ocr import TesseractOCR

    ocr = TesseractOCR(settings)
    calls = []

    def read(image, output, psm, checkpoint, language=None):
        """Регистрирует реальный контракт сегментации без запуска системного OCR."""
        calls.append((psm, language))
        return [{"text": "не", "confidence": 0.95, "box": (5, 5, 25, 20)}]

    ocr._read = read
    with Image.new("RGB", (300, 80), "white") as image:
        before = image.tobytes()
        readings = ocr.recheck(image, tmp_path / "proof", lambda stage: None, "rus")
        assert image.tobytes() == before
    assert len(readings) == 2 and {psm for psm, _ in calls} == {6, 7}
    assert all(language == "rus" for _, language in calls)


def test_missing_geometry_has_its_own_reason():
    """Сопоставленный узел без измеренной области не получает ложную причину отсутствия соответствия."""
    result = short_result()
    result.new.elements[0].words = []
    reread = compare(result.old, result.new)
    assert next(d for d in reread.differences if d.active).unverified_reason == "missing_geometry"


def test_local_proof_does_not_confirm_unread_words_elsewhere_in_the_block():
    """Доказательство одного слова не закрывает другую незавершённую область того же абзаца."""
    initial = short_result()
    initial.new.elements[0].confidence = 0.4
    initial.new.elements[0].words = [
        replace(w, verified=False, confidence=0.2) if "сторон" in w.text else w
        for w in initial.new.elements[0].words
    ]
    result = compare(initial.old, initial.new)
    difference = next(d for d in result.differences if d.active and d.category == "text_modified")
    resolve_ocr(result, difference, "не", "т", confidence=0.95)
    assert difference.status == "confirmed"
    assert statistics(result.differences)["total"] == 1
    assert statistics(result.differences)["unverified"] == 1
    assert result.coverage.new["E000000"] == "unverified"
