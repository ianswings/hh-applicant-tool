"""Тесты fast-path классификации в reply_slot._classify.

Проверяем, что приглашение в мессенджер с ЖИВЫМ рекрутёром (telegram/whatsapp)
перехватывается ДО локальной модели и уходит в эскалацию (route=other), а
боты/формы по ссылке остаются на gemma (route=external_link → авто-отказ).

Тесты НЕ требуют Ollama: классификатор подменяем стабом, который падает, если
его вызвали (значит fast-path не сработал).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hh_applicant_tool.operations.reply_slot import Operation  # noqa: E402


class _BoomClassifier:
    """Стаб: если fast-path не сработал и дошли до gemma — тест должен упасть."""

    def classify(self, msg_text, history=None):  # noqa: D401
        raise AssertionError(
            f"fast-path не перехватил, ушли в gemma: {msg_text!r}"
        )


def _classify(msg_text: str) -> str:
    """Зовёт реальный Operation._classify с подменённым классификатором."""
    op = object.__new__(Operation)  # без __init__: нужен только _classify
    op._classifier = _BoomClassifier()
    return op._classify(msg_text, message_history=[])


# Приглашения в мессенджер к живому человеку → эскалация (other), без gemma.
MESSENGER_CASES = [
    "Напишите, пожалуйста, мне в тг: https://t.me/kda_hr",
    "Удобно ли будет для оперативности перейти в телеграм @marinachw",
    "Давайте продолжим в телеграме, мой ник @hr_anna",
    "Свяжитесь со мной в WhatsApp по номеру в профиле",
    "Пишите в вотсап, так быстрее ответим",
    "Продолжим общение в Telegram?",
]


@pytest.mark.parametrize("msg", MESSENGER_CASES)
def test_messenger_invite_escalates(msg):
    assert _classify(msg) == "other"


def test_bot_link_not_intercepted():
    """Бот-ссылка t.me/...bot НЕ должна эскалироваться — это external_link."""
    msg = "Пообщайтесь с нашим ботом-рекрутёром и ответьте: https://t.me/hr_bot"
    with pytest.raises(AssertionError):
        _classify(msg)  # дошли до gemma → fast-path корректно пропустил бота


def test_form_link_still_external():
    """Форма по ссылке по-прежнему ловится _FORM_LINK_RE как external_link."""
    msg = "Заполните анкету кандидата: https://docs.google.com/forms/d/xyz/viewform"
    assert _classify(msg) == "external_link"
