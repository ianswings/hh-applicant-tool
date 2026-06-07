"""Локальный классификатор сообщений работодателя через Ollama.

Классифицирует ПОСЛЕДНЕЕ сообщение работодателя в один из маршрутов ROUTES
(test_task | external_link | scheduling | document_request | employment_proof |
question_simple | question_expertise | other).

Раннер — Ollama (http://localhost:11434), модель задаётся в config.yaml
(reply.classifier.model). Если Ollama недоступен или модель не скачана —
ПАДАЕМ с понятной ошибкой (по решению: без regex-fallback).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import KW_ONLY, dataclass, field
from typing import Any, Optional

import requests

from .anthropic import AnthropicError

logger = logging.getLogger(__package__)


def strip_json_fences(s: str) -> str:
    """Срезает markdown-обёртку ```json … ``` (баг gemma4 #15595)."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_json_objects(s: str) -> list[dict]:
    """Извлекает ВСЕ top-level JSON-объекты из строки.

    Нужно, т.к. gemma4 нестабильно префиксит ответ рассуждением отдельным
    объектом: '{"reasoning": ...}{"greeting": ...}'. raw_decode проходит подряд.
    """
    s = strip_json_fences(s)
    dec = json.JSONDecoder(strict=False)
    objs: list[dict] = []
    i, n = 0, len(s)
    while i < n:
        j = s.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(s, j)
            if isinstance(obj, dict):
                objs.append(obj)
            i = max(end, j + 1)
        except json.JSONDecodeError:
            i = j + 1
    return objs


def pick_json(s: str, *required_keys: str) -> Optional[dict]:
    """Возвращает объект, где есть ВСЕ required_keys (ответ обычно ПОСЛЕ reasoning).

    None — если ничего подходящего не нашли.
    """
    objs = _parse_json_objects(s)
    for obj in reversed(objs):
        if all(k in obj for k in required_keys):
            return obj
    return None


def _ollama_problem(base_url: str, model: str, session: requests.Session):
    """Возвращает текст проблемы (Ollama недоступен/модель не скачана) или None."""
    try:
        r = session.get(f"{base_url}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except requests.RequestException as ex:
        return (
            f"Ollama недоступен на {base_url}: {ex}. "
            f"Подними его: ollama serve"
        )
    base = model.split(":")[0]
    if not any(n == model or n.startswith(base) for n in names):
        return (
            f"Модель '{model}' не найдена в Ollama. Скачай: ollama pull {model}"
        )
    return None

ROUTES = (
    "test_task_practical",
    "test_task_conceptual",
    "external_link",
    "scheduling",
    "document_request",
    "employment_proof",
    "legal_clearance",
    "question_simple",
    "question_expertise",
    "acknowledgement",
    "other",
)

_SYSTEM = """Ты классифицируешь ПОСЛЕДНЕЕ сообщение работодателя в чате на hh.ru.
Верни ровно один маршрут (route):

- "test_task_practical" — просят ВЫПОЛНИТЬ практическое задание с АРТЕФАКТОМ:
  написать/прислать код, реализовать функцию/сервис/MVP/проект, решить задачу на
  платформе (типа LeetCode), приложить репозиторий, сделать оффлайн-ДЗ.
- "test_task_conceptual" — просят ответить ТЕКСТОМ/РАССУЖДЕНИЕМ, без кода и
  артефактов: «опишите подход/концепцию», «как бы вы решали задачу», «2-3 слайда
  концепции», теоретический разбор. При смешении (нужен и код, и описание) →
  test_task_practical.
- "external_link" — просит перейти по ВНЕШНЕЙ ссылке и что-то сделать: заполнить
  форму/анкету/гуглдок, пройти собеседование с ботом по ссылке. (НО ссылка на
  видеозвонок/созвон — это "scheduling", не сюда.)
- "scheduling" — приглашение/согласование времени собеседования, созвона, звонка,
  встречи (в т.ч. со ссылкой на zoom/meet/teams/календарь).
- "document_request" — просит ПРИСЛАТЬ/ПОКАЗАТЬ документы: договоры, трудовую
  книжку, скан/копию/фото, паспорт, диплом, справки.
- "employment_proof" — просит ПОДТВЕРДИТЬ НА СЛОВАХ, что опыт официальный / по
  ТК РФ / в трудовой / «белый» стаж (но НЕ просит прислать документы).
- "legal_clearance" — спрашивает про ЮРИДИЧЕСКУЮ ЧИСТОТУ / проверку СБ: судимость,
  задолженности или исполнительные производства в ФССП, банкротство, «чистоту»
  для службы безопасности. (Это НЕ про официальность стажа — то employment_proof.)
- "question_simple" — простой фактический вопрос, ответ на который есть в профиле
  и укладывается в одну фразу: зарплатные ожидания, формат работы (удалёнка/офис),
  город, гражданство, уровень английского, готовность выйти/созвониться, контакты.
- "question_expertise" — вопрос, требующий ИЗЛОЖЕНИЯ ОПЫТА: про проекты, стек,
  технологии, архитектуру, «расскажите про…», как решал задачу, технические
  вопросы на знание. Нужен содержательный ответ по резюме.
- "acknowledgement" — вежливое АВТО-подтверждение получения отклика, БЕЗ вопросов
  и без действия с твоей стороны: «спасибо за отклик/интерес», «рассмотрим/изучим
  резюме», «свяжемся/вернёмся, если подойдёте», «взяли резюме в работу», «HR
  позвонит, если опыт подойдёт», «бот завершил работу, дальше — человек».
  Отвечать НЕ требуется. ВАЖНО: если в сообщении есть ВОПРОС или просьба что-то
  сделать/прислать/созвониться — это НЕ acknowledgement.
- "other" — всё прочее или неясное (оффер, обсуждение условий, что не подходит
  под верхние категории).

Приоритет при смешении: test_task_practical > test_task_conceptual > external_link
> scheduling > document_request > employment_proof > legal_clearance
> question_expertise > question_simple > acknowledgement > other.
Отвечай строго JSON: {"route": "<одно из значений>"}."""


class ClassifierError(Exception):
    pass


@dataclass
class LocalClassifier:
    _: KW_ONLY
    model: str = "gemma4:26b"
    base_url: str = "http://localhost:11434"
    timeout: float = 120.0
    # Бюджет генерации: thinking-моделям нужен запас на мышление + маленький JSON.
    num_predict: int = 2048
    num_ctx: int = 8192       # промпт классификации мал; 8к с запасом
    keep_alive: str = "30s"
    # None = не передавать Ollama (дефолт модели); False = выключить thinking (быстрее).
    think: bool | None = None
    session: requests.Session = field(default_factory=requests.Session)

    def health_check(self) -> None:
        """Проверяет, что Ollama поднят и модель скачана. Иначе ClassifierError."""
        problem = _ollama_problem(self.base_url, self.model, self.session)
        if problem:
            raise ClassifierError(problem)

    def classify(self, message: str, history: Optional[list[str]] = None) -> str:
        """Возвращает один из ROUTES. При сбое Ollama — ClassifierError (падаем)."""
        user = ""
        if history:
            user += "Контекст переписки:\n" + "\n".join(history) + "\n\n"
        user += f"ПОСЛЕДНЕЕ сообщение работодателя:\n{message}\n\nКлассифицируй."

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": {
                "type": "object",
                "properties": {
                    "route": {"type": "string", "enum": list(ROUTES)}
                },
                "required": ["route"],
            },
            "options": {
                "temperature": 0,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
            "keep_alive": self.keep_alive,
            # think НЕ шлём (всегда есть format): баг #15260. thinking остаётся
            # включённым по умолчанию → классификация надёжна (чуть медленнее).
        }
        try:
            r = self.session.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            content = r.json()["message"]["content"]
            route = (pick_json(content, "route") or {}).get("route", "")
        except (requests.RequestException, KeyError, ValueError) as ex:
            raise ClassifierError(
                f"Ошибка классификации Ollama ({self.model}): {ex}"
            ) from ex

        if route not in ROUTES:
            logger.warning("[classify] неожиданный route=%r → other", route)
            route = "other"
        return route


@dataclass
class OllamaChat:
    """Локальный чат через Ollama. Drop-in для ChatAnthropic (те же методы).

    Ошибки оборачивает в AnthropicError — чтобы вызывающий код (cover/questions/
    reply), который ловит AnthropicError, работал без изменений.
    """

    _: KW_ONLY
    model: str = "gemma4:e4b"
    base_url: str = "http://localhost:11434"
    timeout: float = 300.0
    # Thinking-моделям нужен большой бюджет (мышление + ответ). max_tokens из
    # cover/Anthropic мал и заточен под Anthropic — мы его игнорируем.
    num_predict: int = 4096
    # Окно контекста: промпт ~3.5к + num_predict 4к ≈ 7.6к; 12288 с запасом.
    # Дефолт gemma4 (256K) раздувает RAM на гигабайты — поэтому ограничиваем.
    num_ctx: int = 12288
    keep_alive: str = "30s"  # сколько держать модель в RAM после запроса
    think: bool | None = None
    system_prompt: str | None = None
    session: requests.Session = field(default_factory=requests.Session)

    def health_check(self) -> None:
        problem = _ollama_problem(self.base_url, self.model, self.session)
        if problem:
            raise AnthropicError(problem)

    def complete(self, message: str) -> str:
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": message})
        return self._generate(messages)

    def complete_with_caching(
        self, system, user, *, prefill=None, max_tokens=None, temperature=None,
        schema=None,
    ) -> str:
        sys_text = (
            "\n\n".join(b["text"] for b in system)
            if isinstance(system, list)
            else str(system)
        )
        messages = [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": user},
        ]
        # fmt задан → _generate сам НЕ шлёт think (баг #15260): схема работает,
        # thinking при этом остаётся включённым по умолчанию модели.
        return self._generate(
            messages,
            fmt=schema or "json",
            temperature=0.3 if temperature is None else temperature,
        )

    def solve_captcha(
        self, image_data: bytes, *, media_type: str = "image/png"
    ) -> str:
        b64 = base64.b64encode(image_data).decode("utf-8")
        messages = [
            {
                "role": "user",
                "content": "Распознай текст на изображении. Верни ТОЛЬКО текст.",
                "images": [b64],
            }
        ]
        # OCR: thinking не нужен, бюджет маленький.
        return self._generate(
            messages, temperature=0.0, num_predict=64, think=False
        ).strip()

    def _generate(
        self, messages, *, fmt=None, temperature=0.0, num_predict=None, think=None
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict or self.num_predict,
                "num_ctx": self.num_ctx,
            },
            "keep_alive": self.keep_alive,
        }
        if fmt:
            payload["format"] = fmt
            # При format НЕ шлём think (баг Ollama #15260: think=false ломает
            # format, think=true льёт reasoning в content; омит → format работает,
            # а модель всё равно думает по умолчанию).
        else:
            eff_think = think if think is not None else self.think
            if eff_think is not None:
                payload["think"] = eff_think
        try:
            r = self.session.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "")
        except (requests.RequestException, KeyError, ValueError) as ex:
            raise AnthropicError(f"Ollama error ({self.model}): {ex}") from ex
        if not content:
            raise AnthropicError(
                f"Ollama вернул пустой content (model={self.model}); "
                f"подними num_predict."
            )
        return strip_json_fences(content)
