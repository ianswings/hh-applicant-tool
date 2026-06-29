"""Отклик со слот-шаблоном cover letter (Anthropic) и YAML-резолвером анкет.

Сабкласс их Operation из apply_vacancies.py. Переопределяет только два seam'а:
- `_generate_letter` — наш build_cover_letter (слот-шаблон + prompt caching);
- `_solve_test_task` — наш QuestionResolver (regex YAML → LLM-фолбэк).

Всё остальное (поиск, отклик, капча, фильтры) наследуется без изменений.
См. MIGRATION_CONTEXT.md, Этап 2.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..ai.anthropic import ChatAnthropic
from ..ai.factory import make_chat
from ..our_extensions import load_config, load_resume
from ..our_extensions.cover import build_cover_letter
from ..our_extensions.notify import TelegramNotifier
from ..our_extensions.questions import QuestionResolver
from ..utils.string import strip_tags
from .apply_vacancies import Namespace as ApplyNamespace
from .apply_vacancies import Operation as ApplyOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

DEFAULT_EXTERNAL_FORM_REPLY = (
    "Готов пройти ваши этапы, но не заполняю внешние формы — "
    "резюме и контакты есть в отклике."
)


class Namespace(ApplyNamespace):
    our_config: Path | None
    our_resume: Path | None


class Operation(ApplyOperation):
    """Отклик со слот-шаблоном (Anthropic) и YAML-резолвером анкет."""

    # Не наследуем алиасы apply/apply-similar — иначе конфликт парсеров с
    # apply_vacancies, который уже их регистрирует.
    __aliases__: list[str] = []

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        super().setup_parser(parser)
        parser.add_argument(
            "--our-config",
            type=Path,
            default=None,
            help="Путь к нашему config.yaml (слоты + rules). "
            "По умолчанию our_extensions/config.yaml.",
        )
        parser.add_argument(
            "--our-resume",
            type=Path,
            default=None,
            help="Путь к resume.md для контекста LLM. "
            "По умолчанию our_extensions/resume.md.",
        )
        parser.add_argument(
            "--review",
            action=argparse.BooleanOptionalAction,
            help="Печатать сгенерированное письмо и ответы на анкету "
            "(превью качества). Совмещай с --dry-run, чтобы ничего не "
            "отправлять.",
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> None | int:
        self._review = bool(getattr(args, "review", False))
        self._cfg = load_config(args.our_config)
        self._cover_cfg = self._cfg.get("cover_letter", {})
        self._profile = self._cfg.get("profile", {})
        self._resume_text = load_resume(args.our_resume)

        llm_cfg = self._cfg.get("llm", {})
        # Основной путь (письма/анкеты) — провайдер из конфига (сейчас local/Ollama).
        self._chat = make_chat(llm_cfg)
        if hasattr(self._chat, "health_check"):
            self._chat.health_check()  # Ollama должен быть поднят — иначе падаем
        if self._chat is None:
            logger.warning(
                "Чат не инициализирован — письма пойдут по fallback, "
                "анкеты резолвятся только regex-правилами (LLM-фолбэк отключён)."
            )

        # Капча — ГИБРИД: всегда через Anthropic vision (точнее локальной OCR),
        # требует ANTHROPIC_API_KEY + прокси. Используется только при появлении капчи.
        self._captcha_chat = ChatAnthropic.from_env(
            model=llm_cfg.get("model", "claude-haiku-4-5"),
            max_tokens=llm_cfg.get("max_tokens", 1000),
            temperature=llm_cfg.get("temperature", 0.0),
        )

        self._resolver = QuestionResolver(
            self._cfg.get("questions", {}), self._chat, profile=self._profile
        )
        self._external_form_reply = (
            self._cfg.get("questions", {}).get("external_form_reply")
            or DEFAULT_EXTERNAL_FORM_REPLY
        ).strip()
        self._notifier = TelegramNotifier.from_env()

        rc = super().run(tool, args)
        self._notify_apply_summary()
        return rc

    def _notify_apply_summary(self) -> None:
        sent = getattr(self, "_total_applied", 0)
        limit = getattr(self, "_limit_reached", False)
        label = self._resume_label()
        prefix = f"📊 apply [{label}]" if label else "📊 apply"
        msg = f"{prefix}: отправлено {sent}" + (
            "; ⛔ дневной лимит исчерпан" if limit else ""
        )
        print(msg)
        if self._notifier and not getattr(self, "dry_run", False):
            self._notifier.send(msg)

    def _resume_label(self) -> str:
        """Название резюме прогона (для отчёта). Пусто, если --resume-id не задан
        (тогда база перебирает все резюме — единой подписи нет)."""
        rid = getattr(self, "resume_id", None)
        if not rid:
            return ""
        try:
            for r in self.tool.get_resumes():
                if r["id"] == rid:
                    return r.get("title") or rid
        except Exception as ex:
            logger.debug("Не удалось получить title резюме %s: %s", rid, ex)
        return rid

    # --- seam 1: cover letter -------------------------------------------------
    def _generate_letter(
        self, vacancy: dict, message_placeholders: dict
    ) -> str:
        full = vacancy
        vacancy_id = vacancy.get("id")
        if vacancy_id:
            try:
                fetched = self.api_client.get(f"/vacancies/{vacancy_id}")
                full = {**vacancy, **fetched}
            except Exception as ex:
                logger.warning(
                    "Не удалось получить полную вакансию %s: %s",
                    vacancy_id,
                    ex,
                )
        letter = build_cover_letter(
            full, self._resume_text, self._chat, self._cover_cfg, profile=self._profile
        )
        if self._review:
            print(f"\n──── ✉️  ПИСЬМО для: {full.get('name', '')} ────")
            print(letter)
            print("─" * 60)
        return letter

    # --- seam 2: test answers -------------------------------------------------
    def _solve_test_task(self, task: dict) -> tuple[str, Any]:
        field_name = f"task_{task['id']}"
        solutions = task.get("candidateSolutions") or []
        question = (task.get("description") or "").strip()
        vacancy_text = self._vacancy_test_context()

        if solutions:
            normalized = [
                {"id": s["id"], "text": strip_tags(s.get("text", ""))}
                for s in solutions
            ]
            sid = self._resolver.pick_solution_id(
                question, normalized, vacancy_text
            )
            if sid is None:
                # Резолвер не дал ответа — берём середину списка, как база.
                sid = solutions[len(solutions) // 2]["id"]
            if self._review:
                chosen = next(
                    (s["text"] for s in normalized if s["id"] == sid), sid
                )
                print(f"  ❓ {question}\n  ☑️  {chosen}")
            return field_name, sid

        # Открытый вопрос со ссылкой на внешнюю форму — её не заполняем,
        # вежливо отказываемся прямо в поле ответа hh.
        if "://" in question and self._external_form_reply:
            if self._review:
                print(
                    f"  ❓ {question}\n  ↪️  (внешняя форма) "
                    f"{self._external_form_reply}"
                )
            return f"{field_name}_text", self._external_form_reply

        answer = self._resolver.resolve(question, vacancy_text)
        if answer is None:
            answer = "Да"
        if self._review:
            print(f"  ❓ {question}\n  ➡️  {answer}")
        return f"{field_name}_text", answer

    # --- seam 3: captcha (ГИБРИД — всегда Anthropic, не локальная модель) -----
    def _recognize_captcha(self, img_bytes: bytes) -> str:
        if self._captcha_chat is not None:
            return self._captcha_chat.solve_captcha(img_bytes)
        # Нет Anthropic-ключа — откатываемся на их OpenAI-captcha из config.json.
        return super()._recognize_captcha(img_bytes)

    def _vacancy_test_context(self) -> str:
        """Короткий контекст вакансии для LLM-фолбэка анкеты."""
        v = getattr(self, "_current_vacancy", None) or {}
        snippet = v.get("snippet") or {}
        parts = [
            v.get("name", ""),
            snippet.get("requirement", "") or "",
            snippet.get("responsibility", "") or "",
        ]
        return " ".join(p for p in parts if p)[:1500]
