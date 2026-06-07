"""Резолвер ответов на вопросы работодателя (анкеты / тесты hh).

Логика: regex-правила из YAML → LLM-фолбэк → None (caller решает: пауза / skip).

Для multiple-choice (task["candidateSolutions"]) используем `pick_solution_id`:
он извлекает тексты вариантов, прогоняет через `resolve`, затем матчит выбранный
текст обратно на solution_id.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..ai.anthropic import AnthropicError, ChatAnthropic

logger = logging.getLogger(__package__)

SENTINEL_LLM = "__llm__"

# Fallback-профиль, если в config.yaml нет секции profile.
DEFAULT_PROFILE = (
    "Профиль: senior Python backend, 6+ лет коммерческого опыта, Москва, "
    "удалёнка/гибрид (не готов к командировкам и релокации), английский C1, "
    "гражданство РФ, ЗП от 400к на руки."
)


def _format_profile(profile: Optional[dict]) -> str:
    """Собрать строку профиля из секции config.yaml::profile."""
    if not profile:
        return DEFAULT_PROFILE
    parts = []
    if profile.get("summary"):
        parts.append(profile["summary"])
    if profile.get("salary"):
        parts.append(f"ЗП {profile['salary']}")
    if profile.get("format"):
        parts.append(profile["format"])
    if profile.get("english"):
        parts.append(f"английский {profile['english']}")
    if profile.get("citizenship"):
        parts.append(f"гражданство {profile['citizenship']}")
    if not parts:
        return DEFAULT_PROFILE
    return "Профиль: " + ", ".join(parts) + "."


class QuestionResolver:
    def __init__(
        self,
        q_cfg: dict,
        chat: Optional[ChatAnthropic] = None,
        profile: Optional[dict] = None,
    ):
        self.rules = q_cfg.get("rules", []) or []
        self.fallback = q_cfg.get("fallback", "pause")
        self.chat = chat
        self._profile_text = _format_profile(profile)

    def resolve(
        self,
        question_text: str,
        vacancy_text: str = "",
        options: Optional[list[str]] = None,
    ) -> Optional[str]:
        """Вернуть ответ строкой. None — "не знаю", caller действует по fallback."""
        for rule in self.rules:
            if re.search(rule["match"], question_text, flags=re.IGNORECASE):
                ans = rule["answer"]
                if ans == SENTINEL_LLM:
                    return self._ask_llm(question_text, vacancy_text, options)
                return self._match_option(ans, options)
        if self.fallback == "llm":
            return self._ask_llm(question_text, vacancy_text, options)
        return None

    def pick_solution_id(
        self,
        question_text: str,
        solutions: list[dict],
        vacancy_text: str = "",
    ) -> Optional[str]:
        """Для multiple-choice: вернуть id выбранного варианта.

        `solutions` — список вида task["candidateSolutions"]: [{"id", "text"}, ...].
        Возвращает None, если резолвер не дал ответа (caller решает пауза/skip).
        """
        if not solutions:
            return None
        texts = [str(s.get("text", "")) for s in solutions]
        answer = self.resolve(question_text, vacancy_text, texts)
        if answer is None:
            return None
        ans_low = answer.strip().lower()
        for s in solutions:
            if str(s.get("text", "")).strip().lower() == ans_low:
                return s.get("id")
        for s in solutions:
            if ans_low and ans_low in str(s.get("text", "")).strip().lower():
                return s.get("id")
        # Бэкап — не падать, берём первый вариант.
        return solutions[0].get("id")

    @staticmethod
    def _match_option(answer: str, options: Optional[list[str]]) -> str:
        if not options:
            return answer
        ans_low = answer.lower()
        for opt in options:
            if opt.lower() == ans_low or ans_low in opt.lower():
                return opt
        return options[0]  # бэкап — не падать

    def _ask_llm(
        self,
        question: str,
        vacancy_text: str,
        options: Optional[list[str]],
    ) -> Optional[str]:
        if self.chat is None:
            logger.warning("[llm] ChatAnthropic не инициализирован, пропуск фолбэка")
            return None
        instruction = (
            "Ты помогаешь соискателю отвечать на вопросы в анкете вакансии. "
            + self._profile_text
            + " Отвечай коротко и по делу, одной фразой или числом. "
            "Без воды и без вступлений."
        )
        prompt = f"{instruction}\n\nВакансия (фрагмент): {vacancy_text[:1500]}\n\nВопрос: {question}"
        if options:
            prompt += (
                "\n\nВарианты ответа (выбери один, верни его дословно):\n"
                + "\n".join(f"- {o}" for o in options)
            )
        try:
            text = self.chat.complete(prompt).strip()
            return self._match_option(text, options)
        except AnthropicError as e:
            logger.warning("[llm] error: %s", e)
            return None
