#!/usr/bin/env python3
"""Сгенерировать сопроводительное письмо ЛОКАЛЬНОЙ моделью (Ollama) по нашим правилам.

Гоняет наш реальный pipeline (cover.build_cover_letter): тот же SYSTEM_PROMPT,
whitelist, profile, resume, repair-валидатор, compliance и remote_note — но LLM
не Anthropic, а локальная модель через Ollama. Цель — сравнить качество с Haiku.

Запуск (из корня репо, poetry-окружение, Ollama поднят):
    poetry run python tools/test_cover_local.py --model gemma4:e4b
    poetry run python tools/test_cover_local.py --model gemma4:26b --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hh_applicant_tool.ai.anthropic import AnthropicError  # noqa: E402
from hh_applicant_tool.our_extensions import (  # noqa: E402
    load_config,
    load_resume,
)
from hh_applicant_tool.our_extensions import cover  # noqa: E402

# JSON-схема слотов письма (Ollama structured output → валидный JSON).
COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string"},
        "stack": {"type": "array", "items": {"type": "string"}},
        "hook": {"type": "string"},
        "match": {"type": "string"},
        "compliance": {"type": "string"},
        "compliance_kind": {"type": "string", "enum": ["word", "answer", ""]},
        "requires_onsite": {"type": "boolean"},
    },
    "required": [
        "greeting", "stack", "hook", "match",
        "compliance", "compliance_kind", "requires_onsite",
    ],
}


@dataclass
class OllamaChat:
    """Drop-in для ChatAnthropic: тот же complete_with_caching, но через Ollama."""

    model: str
    base_url: str = "http://localhost:11434"
    timeout: float = 300.0
    verbose: bool = False
    use_schema: bool = True
    think: bool | None = None  # None = не передавать; False = выключить thinking
    # Бюджет генерации. Для thinking-моделей нужен БОЛЬШОЙ запас (мышление + JSON),
    # поэтому НЕ используем маленький max_tokens из cover (он под Anthropic).
    num_predict: int = 4096
    session: requests.Session = field(default_factory=requests.Session)

    def complete_with_caching(
        self, system, user, *, prefill=None, max_tokens=None, temperature=None
    ) -> str:
        # У Ollama нет cache_control — просто склеиваем system-блоки в один текст.
        sys_text = (
            "\n\n".join(b["text"] for b in system)
            if isinstance(system, list)
            else str(system)
        )
        if not self.use_schema:
            # Без grammar-констрейнта просим JSON словами.
            user = user + (
                "\n\nВерни СТРОГО валидный JSON-объект с полями "
                "greeting, stack, hook, match, compliance, compliance_kind, "
                "requires_onsite. Без markdown, без пояснений."
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": 0.3 if temperature is None else temperature,
                "num_predict": self.num_predict,
            },
            "keep_alive": "10m",
        }
        if self.use_schema:
            payload["format"] = COVER_SCHEMA
        if self.think is not None:
            payload["think"] = self.think
        try:
            r = self.session.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("message", {}).get("content", "")
        except (requests.RequestException, KeyError, ValueError) as ex:
            raise AnthropicError(f"Ollama error: {ex}") from ex
        if self.verbose:
            msg = data.get("message", {})
            print("─── ДИАГНОСТИКА Ollama ───")
            print("done_reason:", data.get("done_reason"))
            print("eval_count:", data.get("eval_count"), "prompt_eval:", data.get("prompt_eval_count"))
            print("message keys:", list(msg.keys()))
            if msg.get("thinking"):
                print("thinking (первые 300):", str(msg["thinking"])[:300])
            print("content (repr):", repr(content)[:500])
            print("──────────────────────────")
        # Пустой content при thinking-модели = бюджета не хватило на ответ.
        if not content:
            hint = (
                "done_reason=length → подними --num-predict"
                if data.get("done_reason") == "length"
                else "попробуй --no-think или --no-schema"
            )
            raise AnthropicError(f"Ollama вернул пустой content ({hint}).")
        return content


# Тестовая вакансия (Сбер, Senior AI Engineer — формат «на месте»/гибрид,
# формальная культура → проверяем greeting=Здравствуйте и remote_note).
VACANCY = {
    "id": None,
    "name": "Senior AI Engineer / AI Architect",
    "employer": {"name": "Сбер"},
    "schedule": {"name": "Полный день"},
    "work_format": [{"name": "На месте работодателя"}],
    "snippet": {
        "requirement": "Python/Java 4+ лет, AI/ML 2+ года, LLM в production: агенты, RAG, guardrails, микросервисы, REST/gRPC.",
        "responsibility": "Проектировать архитектуру AI-систем: LLM-агенты, инфраструктура, инструменты поддержки принятия решений; AI Guild, стандарты AI PDLC.",
    },
    "description": (
        "Мы строим следующее поколение инструментов управления рисками на базе ИИ. "
        "Трайб интегрированных рисков: расчёт резервов, оценка RWA, автоматизация анализа. "
        "Обязанности: проектировать архитектуру AI-систем (LLM-агенты, инфраструктура, core-функции), "
        "технический аудит AI-решений, поддержка смежных команд, внедрение AI PDLC стандартов, "
        "ведение AI Guild (воркшопы, code review, менторинг), технологический радар. "
        "Требования: 4+ лет Python/Java (production), 2+ года AI/ML engineering, LLM в production "
        "(агенты, RAG, guardrails), архитектурные паттерны (микросервисы, event-driven, REST/gRPC), "
        "менторинг. Плюс: финсектор, multi-agent оркестрации, внутренние AI-стандарты. "
        "Условия: комфортный офис рядом с м. Кутузовская, гибридный формат работы, ДМС, ипотека, "
        "годовая премия, обучение."
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="gemma4:e4b", help="Ollama-модель")
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--verbose", action="store_true", help="Диагностика ответа Ollama")
    ap.add_argument(
        "--num-predict",
        type=int,
        default=4096,
        help="Бюджет генерации (thinking-моделям нужно много: мышление + JSON)",
    )
    ap.add_argument(
        "--no-schema",
        action="store_true",
        help="Без JSON-schema грамматики (просим JSON словами)",
    )
    ap.add_argument(
        "--think",
        dest="think",
        action="store_true",
        help="Явно включить thinking",
    )
    ap.add_argument(
        "--no-think",
        dest="think",
        action="store_false",
        help="Явно выключить thinking (для thinking-моделей)",
    )
    ap.set_defaults(think=None)
    args = ap.parse_args()

    # Логи cover (repair/fallback/remote_note) — в stderr, чтобы видеть, что сработало.
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    chat = OllamaChat(
        model=args.model,
        base_url=args.base_url,
        verbose=args.verbose,
        use_schema=not args.no_schema,
        think=args.think,
        num_predict=args.num_predict,
    )

    print(f"🧪 Модель: {args.model}\n")
    t0 = time.monotonic()
    letter = cover.build_cover_letter(
        VACANCY,
        load_resume(),
        chat,
        cfg["cover_letter"],
        profile=cfg.get("profile", {}),
    )
    dt = time.monotonic() - t0

    print("\n========== ПИСЬМО ==========")
    print(letter)
    print("============================")
    print(f"\n⏱  {dt:.1f}s")
    print(
        "Проверь: greeting=Здравствуйте (Сбер→формально), стек только из whitelist, "
        "hook начинается с «У вас/В вашей вакансии», есть абзац про удалёнку "
        "(формат гибрид/офис)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
