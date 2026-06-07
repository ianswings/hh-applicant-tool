"""Генерация cover letter по слот-шаблону.

Архитектура:
1. Базовый текст содержит плейсхолдеры {{greeting}}, {{stack}}, {{hook}}, {{match}}.
2. LLM получает: правила + шаблон + whitelist стека + резюме (system, закэшировано
   через cache_control: ephemeral) и описание вакансии (user).
3. Возвращает JSON: {greeting, stack, hook, match, compliance, compliance_kind}.
4. Валидируем длины/whitelist, склеиваем шаблон. При ошибке — fallback на дефолт.

Агентная строка зашита статикой прямо в template (config.yaml) — гарантированно
в каждом письме. compliance — кодовое слово/ответ, которое вакансия требует
вставить в письмо (с анти-галлюцинацией).

Цена: системка ~3-4к токенов закэширована, дельта — описание вакансии (~500)
+ output (~200). На Haiku с prompt caching отклик стоит копейки.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from ..ai.anthropic import AnthropicError, ChatAnthropic
from ..ai.local import pick_json

logger = logging.getLogger(__package__)

# Параметры генерации слотов (письмо короткое, но с запасом под compliance-ответ).
COVER_MAX_TOKENS = 500
COVER_TEMPERATURE = 0.3

# Пороги валидации. LLM в промпте просим stack 4–7 / hook ≤18 / match ≤28,
# но терпим огрехи до этих значений; грубее — fallback.
HOOK_SOFT_MAX_WORDS = 40
MATCH_SOFT_MAX_WORDS = 60
STACK_MIN = 4           # меньше валидных технологий → fallback
STACK_MAX = 10          # фактический потолок: >10 обрезаем до 10 (LLM просим 7)

# Строгая JSON-schema слотов (для Ollama structured output → надёжный JSON на e4b).
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
        "requires_test_task": {"type": "boolean"},
        "requires_employment_proof": {"type": "boolean"},
    },
    "required": [
        "greeting", "stack", "hook", "match", "compliance",
        "compliance_kind", "requires_onsite", "requires_test_task",
        "requires_employment_proof",
    ],
}

# Гард для remote_note: маркеры офисного/гибридного формата в тексте вакансии.
_ONSITE_RE = re.compile(
    r"гибрид|офис|on-?site|онсайт|на месте|присутстви", re.IGNORECASE
)

# Гард для test_task_note: вакансия требует выполнить тестовое/пробное задание.
_TESTTASK_RE = re.compile(
    r"тестов\w*\s+задани|пробн\w*\s+задани|выполнит\w*\s+(?:тестов|пробн|задани)",
    re.IGNORECASE,
)

# Гард для employment_proof_note: требуют официальный стаж / по ТК / трудовую.
_EMPLOYMENT_PROOF_RE = re.compile(
    r"по\s+ТК|ТК\s*РФ|трудов\w*\s+книж|официальн\w*\s+(?:оформл|трудоустрой|стаж)|"
    r"бел\w*\s+стаж|подтверд\w*\s+стаж",
    re.IGNORECASE,
)

# Гард для compliance: вакансия должна ЯВНО требовать что-то вписать в письмо.
# Иначе compliance — почти наверняка галлюцинация (модель путает с форматом и т.п.).
_COMPLIANCE_HINT_RE = re.compile(
    r"сопроводительн|в\s+письме|в\s+сообщени|кодов\w*\s+слов|"
    r"напиш\w*\s+слов|начни\w*\s+(?:письмо|сообщение)\s+с|"
    r"ответьте\s+на\s+вопрос|укажите\s+в\s+(?:письме|отклике|сообщении)",
    re.IGNORECASE,
)


SYSTEM_PROMPT = """Ты помогаешь senior Python-разработчику адаптировать сопроводительное письмо под конкретную вакансию hh.ru.

Тебе даны:
1. БАЗОВЫЙ ШАБЛОН сопроводительного письма с плейсхолдерами.
2. WHITELIST технологий, с которыми соискатель РЕАЛЬНО работал.
3. РЕЗЮМЕ соискателя.
4. ПРОФИЛЬ соискателя (зарплата, формат работы, английский, гражданство и т.п.).

Ты должен вернуть JSON c полями: greeting, stack, hook, match, compliance, compliance_kind, requires_onsite, requires_test_task, requires_employment_proof.

ПРАВИЛА:

**greeting** — приветствие.
- По умолчанию: "Привет"
- Меняй на "Здравствуйте" только если в вакансии видны маркеры формальной культуры:
  банк/госкомпания/страховая (Сбер, ВТБ, Газпром, Альфа, Росатом, и т.п.),
  фразы "уважаемые кандидаты", "направить резюме", упоминание формального дресс-кода, KPI, корпоративных процедур.

**stack** — массив технологий через запятую, которые попадут в письмо.
- Длина: СТРОГО от 4 до 7 элементов, НЕ больше 7.
- Логика выбора:
  (а) Берём пересечение whitelist'а соискателя и стека, упомянутого в вакансии.
  (б) Если пересечения < 4 — добиваем элементами из whitelist'а, релевантными по смыслу вакансии.
  (в) Минимум всегда включает: "Python", "FastAPI (async)", "PostgreSQL".
- Названия — ДОСЛОВНО как в whitelist'е (регистр и форматирование).
- НЕ добавлять технологии вне whitelist'а в это поле.

**hook** — одно конкретное предложение, что зацепило ИМЕННО в этой вакансии.
- СТРОГО одно предложение, не длиннее 18 слов.
- Конкретика: домен / задача / масштаб / технологический вызов.
- Обращайся напрямую к вакансии/компании ОТ ВТОРОГО ЛИЦА: начинай с "У вас…",
  "В вашем продукте…", "В вашей вакансии…". Должно быть очевидно, что это отклик
  именно на эту вакансию, а не общая фраза.
- Никаких штампов: "интересный продукт", "сильная команда", "амбициозные задачи".
- Если в вакансии не за что зацепиться (общая формулировка) — "Заинтересовала ваша вакансия."

**match** — одно предложение про релевантный опыт.
- СТРОГО одно предложение, не длиннее 28 слов.
- ПРАВИЛО: называй технологии ДОСЛОВНО как в вакансии. Никаких размытых формулировок
  ("опыт с асинхронными фреймворками", "знаком со схожими решениями", "близко по сути").
- Если технологии из вакансии есть в whitelist'е — пиши про них от первого лица, опираясь на резюме.
- Если технологий из вакансии нет в whitelist'е — ВСЁ РАВНО назови их в match как "работал с X, Y, Z"
  (соискатель осознанно выбрал такую стратегию).
- Опирайся на резюме для конкретики (проекты, цифры) когда можешь.

**compliance** и **compliance_kind** — выполнение явных требований вакансии к письму.
- Если вакансия требует вставить КОНКРЕТНОЕ слово/фразу (напр. "напишите слово X",
  "начните письмо со слова X", "укажите кодовое слово"):
  compliance_kind = "word", compliance = это слово/фраза ДОСЛОВНО как в вакансии.
- Если вакансия требует ОТВЕТИТЬ на вопрос прямо в письме (напр. "ответьте, почему…",
  "готовы ли вы к…", "укажите зарплатные ожидания в письме"):
  compliance_kind = "answer", compliance = краткий ответ (≤ 30 слов), опираясь на
  ПРОФИЛЬ и РЕЗЮМЕ соискателя.
- Если таких требований в тексте вакансии НЕТ: compliance_kind = "", compliance = "".
- НЕ выдумывай требование, которого нет в тексте вакансии.
- Формат работы (офис/гибрид/удалёнка) — это НЕ compliance: для него есть отдельное
  поле requires_onsite. НИКОГДА не пиши про формат работы в compliance.

**requires_onsite** — булево (true/false).
- true ТОЛЬКО если вакансия требует работу в офисе или гибридный формат
  (поле "График"/"Формат работы" = офис/гибрид, либо в тексте явно "гибрид",
  "в офисе", "on-site", присутствие на месте).
- false, если формат полностью удалённый ИЛИ формат не указан.
- НЕ путай с упоминанием адреса офиса — важно именно ТРЕБОВАНИЕ присутствия.

**requires_test_task** — булево (true/false).
- true, если вакансия требует выполнить ТЕСТОВОЕ/пробное ЗАДАНИЕ (написать код,
  решить задачу и прислать): в тексте «тестовое задание», «пробное задание»,
  «выполнить задание».
- false, если такого требования нет.
- Это НЕ про тесты-опросники hh и НЕ про вопросы — только про задание, которое
  нужно выполнить и прислать.

**requires_employment_proof** — булево (true/false).
- true, если вакансия требует ОФИЦИАЛЬНЫЙ стаж / оформление по ТК РФ / запись в
  трудовой книжке / «белый» стаж («по ТК», «трудовая книжка», «официальное
  трудоустройство», «белый стаж», «подтвердить стаж»).
- false, если такого требования нет.

ФОРМАТ ВЫВОДА — строго валидный JSON, без markdown-обёрток, без комментариев:

{"greeting": "...", "stack": ["...", "..."], "hook": "...", "match": "...", "compliance": "...", "compliance_kind": "", "requires_onsite": false, "requires_test_task": false, "requires_employment_proof": false}
"""


CONTEXT_TEMPLATE = """БАЗОВЫЙ ШАБЛОН:
{template}

---

WHITELIST технологий соискателя (использовать ДОСЛОВНЫЕ названия):
{whitelist}

---

ПРОФИЛЬ соискателя (используй для ответов на вопросы вакансии в compliance):
{profile}

---

РЕЗЮМЕ:

{resume}
"""


PLACEHOLDERS = ("greeting", "stack", "hook", "match")


def build_cover_letter(
    vacancy: dict,
    resume_text: str,
    chat: Optional[ChatAnthropic],
    cfg: dict,
    profile: Optional[dict] = None,
) -> str:
    """Главная точка входа. Возвращает готовый текст сопроводительного письма.

    `cfg` — секция cover_letter из config.yaml (template, stack_whitelist,
    default_stack). `profile` — секция profile (ЗП, формат, английский и т.п.),
    нужна LLM для ответов на вопросы вакансии (compliance). `chat` может быть
    None (нет API-ключа) — тогда сразу fallback.
    """
    template: str = cfg["template"]
    whitelist: list[str] = cfg["stack_whitelist"]
    vacancy_text = _vacancy_to_text(vacancy)

    slots = None
    if chat is not None:
        slots = _ask_llm_for_slots(
            vacancy_text=vacancy_text,
            whitelist=whitelist,
            resume_text=resume_text,
            template=template,
            profile=profile or {},
            chat=chat,
        )

    if slots is None or not _repair_and_validate(slots, whitelist):
        return _fallback(template, cfg)

    _sanitize_compliance(slots, vacancy_text)
    _resolve_remote_note(slots, cfg, vacancy_text)
    _resolve_test_task_note(slots, cfg, vacancy_text)
    _resolve_employment_proof_note(slots, cfg, vacancy_text)
    return _render(template, slots)


def _vacancy_to_text(v: dict) -> str:
    name = v.get("name", "")
    snippet = v.get("snippet") or {}
    description = v.get("description") or ""
    if description:
        description = re.sub(r"<[^>]+>", " ", description)
        description = re.sub(r"\s+", " ", description).strip()
    parts = [
        f"Название: {name}",
        f"Компания: {(v.get('employer') or {}).get('name', '')}",
        f"Требования: {snippet.get('requirement', '')}",
        f"Обязанности: {snippet.get('responsibility', '')}",
    ]
    schedule = (v.get("schedule") or {}).get("name")
    if schedule:
        parts.append(f"График: {schedule}")
    work_format = [
        w.get("name", "")
        for w in (v.get("work_format") or [])
        if w.get("name")
    ]
    if work_format:
        parts.append(f"Формат работы: {', '.join(work_format)}")
    if description:
        parts.append(f"Описание: {description[:3000]}")
    return "\n".join(parts)


def _ask_llm_for_slots(
    vacancy_text: str,
    whitelist: list[str],
    resume_text: str,
    template: str,
    profile: dict,
    chat: ChatAnthropic,
) -> Optional[dict]:
    context = CONTEXT_TEMPLATE.format(
        template=template.strip(),
        whitelist="\n".join(f"- {t}" for t in whitelist),
        profile=_profile_to_text(profile),
        resume=resume_text.strip(),
    )
    system = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": context,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    user = f"ОПИСАНИЕ ВАКАНСИИ:\n\n{vacancy_text}\n\nВерни JSON."

    raw = ""
    try:
        raw = chat.complete_with_caching(
            system,
            user,
            prefill="{",
            max_tokens=COVER_MAX_TOKENS,
            temperature=COVER_TEMPERATURE,
            schema=COVER_SCHEMA,
        )
    except AnthropicError as e:
        logger.warning("[cover] llm error: %s", e)
        return None
    # Устойчивый разбор: gemma4 может префиксить рассуждение отдельным объектом —
    # берём объект, где есть слоты письма.
    slots = pick_json(raw, "greeting", "stack")
    if slots is None:
        logger.warning("[cover] не извлёк слоты из ответа; raw=%r", raw[:200])
        return None
    return slots


def _repair_and_validate(slots: dict, whitelist: list[str]) -> bool:
    """Чинит мелкие огрехи slots на месте; False при грубых нарушениях → fallback.

    Чиним молча (debug): greeting → "Привет"; стек — выкидываем не-whitelist,
    обрезаем до STACK_MAX. Терпим: hook/match до *_SOFT_MAX_WORDS.
    Fallback (warning): нет/пустое обязательное поле; стек < STACK_MIN валидных;
    hook/match длиннее soft-порога.
    """
    for k in PLACEHOLDERS:
        if k not in slots:
            logger.warning("[cover] нет обязательного поля %s — fallback", k)
            return False

    # greeting — чиним
    if slots["greeting"] not in ("Привет", "Здравствуйте"):
        logger.debug("[cover] greeting %r → 'Привет'", slots.get("greeting"))
        slots["greeting"] = "Привет"

    # stack — чистим от не-whitelist, проверяем минимум, обрезаем по максимуму
    stack = slots.get("stack") or []
    if not isinstance(stack, list):
        logger.warning("[cover] stack не список — fallback")
        return False
    wl_lower = {s.lower() for s in whitelist}
    cleaned = [
        t for t in stack if isinstance(t, str) and t.lower() in wl_lower
    ]
    if len(cleaned) != len(stack):
        logger.debug(
            "[cover] выкинул %d не-whitelist из стека", len(stack) - len(cleaned)
        )
    if len(cleaned) < STACK_MIN:
        # Добиваем из whitelist (Python/FastAPI/PostgreSQL идут первыми) — письмо
        # сохраняем вместо fallback; технологии реальные.
        logger.debug(
            "[cover] стек < %d (%d) — добиваю из whitelist", STACK_MIN, len(cleaned)
        )
        for t in whitelist:
            if t not in cleaned:
                cleaned.append(t)
            if len(cleaned) >= STACK_MIN:
                break
    if len(cleaned) > STACK_MAX:
        logger.debug("[cover] стек %d → обрезал до %d", len(cleaned), STACK_MAX)
        cleaned = cleaned[:STACK_MAX]
    slots["stack"] = cleaned

    # hook / match — пустые недопустимы; длина мягкая до soft-порога
    for key, soft_max in (
        ("hook", HOOK_SOFT_MAX_WORDS),
        ("match", MATCH_SOFT_MAX_WORDS),
    ):
        text = str(slots.get(key) or "").strip()
        if not text:
            logger.warning("[cover] пустой %s — fallback", key)
            return False
        n_words = len(text.split())
        if n_words > soft_max:
            logger.warning(
                "[cover] %s слишком длинный (%d сл. > %d) — fallback",
                key,
                n_words,
                soft_max,
            )
            return False
        slots[key] = text

    return True


def _profile_to_text(profile: dict) -> str:
    if not profile:
        return "(нет данных)"
    labels = {
        "summary": "Кратко",
        "salary": "Зарплата",
        "format": "Формат работы",
        "english": "Английский",
        "citizenship": "Гражданство",
    }
    lines = [f"- {labels.get(k, k)}: {v}" for k, v in profile.items() if v]
    return "\n".join(lines) if lines else "(нет данных)"


def _sanitize_compliance(slots: dict, vacancy_text: str) -> None:
    """Чистит compliance: выкидывает галлюцинации и слишком длинные ответы.

    Мутирует slots на месте. Невалидное требование не роняет письмо — просто
    отбрасывается (compliance="").
    """
    compliance = (slots.get("compliance") or "").strip()
    kind = (slots.get("compliance_kind") or "").strip().lower()

    def _drop():
        slots["compliance"] = ""
        slots["compliance_kind"] = ""

    if not compliance or kind not in ("word", "answer"):
        _drop()
        return

    # Анти-галлюцинация: без явного требования «вставьте … в письмо» — выкидываем
    # (модель часто ошибочно лепит compliance про формат работы и т.п.).
    if not _COMPLIANCE_HINT_RE.search(vacancy_text):
        logger.warning(
            "[cover] compliance без явного требования в вакансии — выкидываю "
            "(%s: %r)",
            kind,
            compliance[:60],
        )
        _drop()
        return

    if kind == "word":
        # Анти-галлюцинация: слово должно реально встречаться в тексте вакансии.
        if compliance.lower() not in vacancy_text.lower():
            logger.warning(
                "[cover] compliance-слово %r не найдено в вакансии — выкидываю",
                compliance,
            )
            _drop()
            return
    elif kind == "answer":
        if len(compliance.split()) > 45:
            logger.warning("[cover] compliance-ответ слишком длинный — выкидываю")
            _drop()
            return

    slots["compliance"] = compliance
    slots["compliance_kind"] = kind


def _resolve_remote_note(slots: dict, cfg: dict, vacancy_text: str) -> None:
    """Ставит slots['remote_note'] только если вакансия требует офис/гибрид.

    Двойная защита: булев флаг requires_onsite от LLM И regex-гард по тексту
    вакансии (иначе LLM мог ложно решить, что нужен онсайт).
    """
    slots["remote_note"] = ""
    note_text = (cfg.get("remote_note") or "").strip()
    if not note_text or not slots.get("requires_onsite"):
        return
    if not _ONSITE_RE.search(vacancy_text):
        logger.debug(
            "[cover] requires_onsite=true, но маркеров формата нет — без remote_note"
        )
        return
    slots["remote_note"] = note_text


def _resolve_test_task_note(slots: dict, cfg: dict, vacancy_text: str) -> None:
    """Ставит slots['test_task_note'] только если вакансия требует тестовое.

    Двойная защита: флаг requires_test_task от LLM И regex-гард по тексту.
    """
    slots["test_task_note"] = ""
    note_text = (cfg.get("test_task_note") or "").strip()
    if not note_text or not slots.get("requires_test_task"):
        return
    if not _TESTTASK_RE.search(vacancy_text):
        logger.debug(
            "[cover] requires_test_task=true, но маркеров нет — без test_task_note"
        )
        return
    slots["test_task_note"] = note_text


def _resolve_employment_proof_note(
    slots: dict, cfg: dict, vacancy_text: str
) -> None:
    """Ставит slots['employment_proof_note'] только если требуют офиц. стаж/ТК."""
    slots["employment_proof_note"] = ""
    note_text = (cfg.get("employment_proof_note") or "").strip()
    if not note_text or not slots.get("requires_employment_proof"):
        return
    if not _EMPLOYMENT_PROOF_RE.search(vacancy_text):
        logger.debug(
            "[cover] requires_employment_proof=true, но маркеров нет — пропуск"
        )
        return
    slots["employment_proof_note"] = note_text


def _render(template: str, slots: dict) -> str:
    stack_str = ", ".join(slots["stack"])
    out = template
    out = out.replace("{{greeting}}", slots["greeting"])
    out = out.replace("{{stack}}", stack_str)
    out = out.replace("{{hook}}", slots["hook"])
    out = out.replace("{{match}}", slots["match"])
    out = out.replace("{{remote_note}}", slots.get("remote_note") or "")
    out = out.replace("{{test_task_note}}", slots.get("test_task_note") or "")
    out = out.replace(
        "{{employment_proof_note}}", slots.get("employment_proof_note") or ""
    )
    # Схлопываем пустые абзацы (например, когда условные строки пустые).
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    compliance = (slots.get("compliance") or "").strip()
    kind = (slots.get("compliance_kind") or "").strip().lower()
    if compliance:
        if kind == "word":
            # Кодовое слово — отдельной строкой в самое начало
            # (покрывает требование "начните письмо со слова X").
            out = f"{compliance}\n\n{out}"
        elif kind == "answer":
            # Ответ на вопрос вакансии — постскриптумом.
            out = f"{out}\n\nP.S. {compliance}"
    return out


def _fallback(template: str, cfg: dict) -> str:
    """Если LLM упала или валидация не прошла — нейтральный CL по дефолту."""
    default_stack = cfg.get(
        "default_stack",
        ["Python", "FastAPI (async)", "PostgreSQL", "Redis", "Kafka", "Docker"],
    )
    return _render(
        template,
        {
            "greeting": "Привет",
            "stack": default_stack,
            "hook": "Заинтересовала ваша вакансия.",
            "match": "Готов обсудить, какие задачи и стек у команды.",
        },
    )
