"""Юнит-тесты captcha circuit-breaker: счётчик капч подряд, сброс, порог.

Логика чистая (без сети): _note_captcha инкрементит и бросает
CaptchaCircuitBreaker при достижении порога; _reset_captcha обнуляет серию.
"""

import pytest

from hh_applicant_tool.operations.apply_vacancies import (
    CaptchaCircuitBreaker,
    Operation,
)


def _op(threshold: int) -> Operation:
    # Без __init__ (он требует tool/args) — нам нужны только атрибуты счётчика.
    op = object.__new__(Operation)
    op._consecutive_captchas = 0
    op._captcha_threshold = threshold
    return op


def test_trips_after_threshold():
    op = _op(3)
    op._note_captcha()  # 1
    op._note_captcha()  # 2
    with pytest.raises(CaptchaCircuitBreaker):
        op._note_captcha()  # 3 — порог достигнут


def test_reset_breaks_streak():
    op = _op(3)
    op._note_captcha()
    op._note_captcha()
    op._reset_captcha()  # успешное действие посреди серии
    assert op._consecutive_captchas == 0
    # после сброса снова можно набрать почти до порога без срабатывания
    op._note_captcha()
    op._note_captcha()
    assert op._consecutive_captchas == 2


def test_disabled_when_zero():
    op = _op(0)  # 0 = выключено
    for _ in range(20):
        op._note_captcha()  # не должно бросать
    assert op._consecutive_captchas == 20
