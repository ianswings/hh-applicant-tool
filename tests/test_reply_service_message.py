"""Тесты фильтра служебных hh-вставок (виджет «отзывы о работодателе»).

Проверяем:
- детект служебного сообщения по тексту;
- выбор значимого сообщения = последнее НЕ-служебное employer ПОСЛЕ нашего;
- анти-дубль: если после нашего ответа пришла только служебка — НЕ откатываемся
  к уже отвеченному вопросу (иначе был бы повторный ответ).
"""

import re

from hh_applicant_tool.operations.reply_slot import (
    DEFAULT_SERVICE_PATTERNS,
    Operation,
)

SERVICE = (
    "**У работодателя 26374 отзыва и рейтинг 4**\n\n"
    "Изучите мнения тех, кто работал здесь — это поможет принять верное "
    "решение на следующем этапе"
)


def _op() -> Operation:
    op = object.__new__(Operation)
    op._service_patterns = [
        re.compile(p, re.IGNORECASE) for p in DEFAULT_SERVICE_PATTERNS
    ]
    return op


def _msg(pt: str, text: str, mid: str) -> dict:
    return {"author": {"participant_type": pt}, "text": text, "id": mid}


def test_is_service_message():
    op = _op()
    assert op._is_service_message(SERVICE)
    assert not op._is_service_message("Когда вам удобно созвониться?")
    assert not op._is_service_message(None)


def test_pick_skips_service_returns_real_question():
    # Кейс СБЕР: cover(я) → реальный вопрос(emp) → служебка(emp)
    op = _op()
    items = [
        _msg("applicant", "cover", "1"),
        _msg("employer", "Перейдите по ссылке к ГигаРекрутеру https://x", "2"),
        _msg("employer", SERVICE, "3"),
    ]
    sig = op._pick_significant_message(items, items[-1])
    assert sig["id"] == "2"  # реальный вопрос, а не служебка


def test_pick_no_dupe_after_our_answer():
    # cover(я) → вопрос(emp) → наш ответ(я) → служебка(emp)
    # После нашего ответа реального вопроса НЕТ → значимое = служебка (default),
    # её _classify заглушит. НЕ откатываемся к уже отвеченному вопросу.
    op = _op()
    items = [
        _msg("applicant", "cover", "1"),
        _msg("employer", "вопрос", "2"),
        _msg("applicant", "наш ответ", "3"),
        _msg("employer", SERVICE, "4"),
    ]
    sig = op._pick_significant_message(items, items[-1])
    assert sig["id"] == "4"  # служебка, НЕ "2"


def test_pick_plain_question_unchanged():
    op = _op()
    items = [
        _msg("applicant", "cover", "1"),
        _msg("employer", "Расскажите про опыт с Kafka", "2"),
    ]
    sig = op._pick_significant_message(items, items[-1])
    assert sig["id"] == "2"
