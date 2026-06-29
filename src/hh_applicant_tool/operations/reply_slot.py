"""Ведение переписки с работодателями: локальная классификация + роутинг ответа.

Сабкласс их Operation из reply_employers.py. Пайплайн на каждый чат, где
последним написал работодатель:
  external_link      → авто фикс-отказ (по ссылкам/формам не ходим);
  test_task          → авто фикс-отказ (тестовые не делаем);
  employment_proof   → авто фикс-ответ (оформление по ТК);
  scheduling/other/document_request → эскалация тебе (в LLM не шлём);
  question_simple    → авто-ответ ЛОКАЛЬНОЙ моделью (факты из профиля);
  question_expertise → авто-ответ через Haiku (изложение опыта; кэш, гарды).
Если нужная модель недоступна — эскалация (не молчим).

Классификация — локально (Ollama, см. ai/local.py). Если Ollama недоступен —
падаем (по решению, без regex-fallback). См. MIGRATION_CONTEXT.md, Этап 5.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..ai.anthropic import ChatAnthropic
from ..ai.base import AIError
from ..api.errors import ApiError
from ..ai.factory import make_chat
from ..ai.local import LocalClassifier, pick_json
from ..our_extensions import load_config, load_resume
from ..our_extensions.cover import _profile_to_text, _vacancy_to_text
from ..our_extensions.notify import TelegramNotifier
from ..utils.date import parse_api_datetime
from .reply_employers import Namespace as ReplyNamespace
from .reply_employers import Operation as ReplyOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

# Дедуп: chat_id → {id, h} последнего обработанного сообщения работодателя. Чтобы
# не гонять классификатор и не отвечать повторно. Храним и id, и хэш текста —
# боты-эхо шлют тот же текст с НОВЫМ id, ловим их по хэшу.
SEEN_PATH = Path(".state/reply_seen.json")


def _norm_text(s: str | None) -> str:
    """Нормализует текст для сравнения (эхо-детект, дедуп по содержанию)."""
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def _text_hash(s: str | None) -> str | None:
    """Короткий хэш нормализованного текста. None для пустого."""
    n = _norm_text(s)
    return hashlib.sha1(n.encode("utf-8")).hexdigest()[:16] if n else None

# Fast-path: явная просьба заполнить форму/анкету/тест по ссылке (НЕ видеозвонок).
_FORM_LINK_RE = re.compile(
    r"forms\.gle|docs\.google\.com/forms|forms\.yandex|surveymonkey|typeform|"
    r"google\.com/forms|анкет\w*\s+по\s+ссылк|заполнит\w*\s+(?:форм|анкет|опрос)|"
    r"пройдит\w*\s+по\s+ссылк|тест\w*\s+по\s+ссылк",
    re.IGNORECASE,
)

# Fast-path: приглашение продолжить общение в мессенджере с ЖИВЫМ рекрутёром
# (telegram/whatsapp). Это НЕ external_link (форма/бот по ссылке), а контакт с
# человеком → эскалируем тебе (сам решаешь, идти ли в личку). gemma путает такие
# сообщения с external_link («перейти + ссылка») и шлёт авто-отказ — перехватываем
# детерминированно, ДО локальной модели. Боты/формы отсекаем _MESSENGER_BOT_RE.
_MESSENGER_RE = re.compile(
    r"t\.me/|telegram|телег\w*|\bтг\b|whats\s?app|вотсап\w*|ватсап\w*|вацап\w*",
    re.IGNORECASE,
)
# Если рядом эти признаки — это бот/форма/анкета (а не живой человек): оставляем
# gemma, она отнесёт к external_link → авто-отказ. «_bot»/«…bot» в хэндле — бот.
_MESSENGER_BOT_RE = re.compile(
    r"\bбот\w*|\bbot\b|_bot|\w*bot\b|форм\w*|анкет\w*|опрос\w*",
    re.IGNORECASE,
)

# Служебные hh-вставки: виджет «отзывы о работодателе», который hh добавляет
# в чат как employer-сообщение ПОВЕРХ реального вопроса. Структурного признака
# в API нет (participant_type=employer как у реального) — детект только по тексту.
# Привязка к уникальной фразе виджета → near-zero false-positive.
DEFAULT_SERVICE_PATTERNS = [
    r"Изучите мнения тех, кто работал здесь",
    r"У работодателя\s+\d+\s+отзыв",
]

DEFAULT_EXTERNAL_LINK_REPLY = (
    "Спасибо! По внешним ссылкам не перехожу и формы/боты не заполняю. "
    "Готов ответить на все вопросы голосом на собеседовании — резюме и контакты "
    "есть в отклике."
)
DEFAULT_TEST_TASK_REPLY = (
    "Спасибо за интерес к моей кандидатуре! Тестовые задания я не выполняю — "
    "ценю и своё, и ваше время. При этом с радостью подтвержу компетенции на "
    "собеседовании: готов к лайв-кодингу, разбору архитектуры и любым вопросам "
    "по реальному опыту."
)
DEFAULT_EMPLOYMENT_PROOF_REPLY = (
    "По оформлению: последние 3 года работаю официально по ТК РФ. Более ранний "
    "опыт — по договорам ГПХ, оригиналы на руках."
)
DEFAULT_LEGAL_CLEARANCE_REPLY = (
    "Судимостей, задолженностей в ФССП и банкротств не имею — "
    "юридически полностью чист."
)
DEFAULT_REPLY_SYSTEM = (
    "Ты ведёшь переписку от лица соискателя на hh.ru. Отвечай кратко, вежливо, "
    "по делу, без markdown. Только удалёнка (офис/гибрид/командировки/релокацию "
    "не принимать). ЗП — из профиля. Без твёрдых обещаний. Подпись — только имя "
    "«Ян». Если данных нет в профиле/резюме или нужно решение соискателя — "
    "escalate=true. "
    'Верни JSON: {"reply": "...", "escalate": false}.'
)
DEFAULT_REPLY_INSTRUCTION = "Ответь на последнее сообщение работодателя."
DEFAULT_CONCEPTUAL_INSTRUCTION = (
    "Это концептуальное тестовое задание. Дай развёрнутый структурированный "
    "технический разбор: как бы ты решал задачу — подход, ключевые шаги, "
    "инструменты и технологии, на что обратить внимание. Несколько абзацев, по "
    "делу. Только факты из резюме, без выдумок."
)

# Строгая JSON-schema ответа (для Ollama structured output).
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "escalate": {"type": "boolean"},
    },
    "required": ["reply", "escalate"],
}


class Namespace(ReplyNamespace):
    our_config: Path | None
    our_resume: Path | None
    review: bool


class Operation(ReplyOperation):
    """Переписка с работодателями: классификация (Ollama) + ответ (Anthropic)."""

    __aliases__: list[str] = []

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        super().setup_parser(parser)
        parser.add_argument(
            "--our-config",
            type=Path,
            default=None,
            help="Путь к нашему config.yaml. По умолчанию our_extensions/config.yaml.",
        )
        parser.add_argument(
            "--our-resume",
            type=Path,
            default=None,
            help="Путь к resume.md. По умолчанию our_extensions/resume.md.",
        )
        parser.add_argument(
            "--review",
            action=argparse.BooleanOptionalAction,
            help="Печатать класс и сгенерированный ответ/эскалацию. Совмещай с --dry-run.",
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> None:
        self._setup(tool, args)       # базовая инициализация reply_employers
        self._setup_slot(args)        # наши расширения
        self.reply_employers()
        self._save_seen()
        self._notify_reply_summary()

    def _setup_slot(self, args: Namespace) -> None:
        # Глушим шаблонную ветку, чтобы шёл наш AI-роутинг.
        self.reply_message = ""
        self._review = bool(getattr(args, "review", False))
        self._notifier = TelegramNotifier.from_env()
        # Счётчики прогона с разбивкой по резюме (title → answered/escalated/
        # acknowledged). Отклики идут двумя резюме — в отчёт пишем, по какому что.
        self._stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"answered": 0, "escalated": 0, "acknowledged": 0}
        )
        self._seen = self._load_seen()  # дедуп уже обработанных сообщений

        # Базовый _setup жёстко ставит resume_id = first_resume_id() → reply
        # отвечал бы только в чатах ОДНОГО резюме. Сбрасываем, чтобы вести
        # переписку по ВСЕМ опубликованным резюме (отклики идут двумя). --resume-id
        # (если задан явно) сужает обратно к одному.
        self.resume_id = getattr(args, "resume_id", None)

        cfg = load_config(args.our_config)
        self._profile = cfg.get("profile", {})
        self._resume_text = load_resume(args.our_resume)
        reply_cfg = cfg.get("reply", {})

        llm_cfg = cfg.get("llm", {})
        # Ответы по сути — провайдер из конфига (сейчас local/Ollama).
        self._chat = make_chat(llm_cfg)
        if hasattr(self._chat, "health_check"):
            self._chat.health_check()  # Ollama должен быть поднят — иначе падаем
        if self._chat is None:
            logger.warning(
                "Чат не инициализирован — вопросы будут эскалироваться, "
                "авто-ответы по сути отключены."
            )

        # Экспертные вопросы (изложение опыта) → Haiku через прокси. None, если
        # нет ANTHROPIC_API_KEY → деградация: такие вопросы эскалируются, не молчим.
        self._haiku = ChatAnthropic.from_env(
            model=llm_cfg.get("model", "claude-haiku-4-5"),
            max_tokens=llm_cfg.get("max_tokens", 400),
            temperature=llm_cfg.get("temperature", 0.4),
        )
        if self._haiku is None:
            logger.warning(
                "[reply] Haiku недоступен (нет ANTHROPIC_API_KEY) — экспертные "
                "вопросы будут эскалироваться."
            )

        # Позиционирование ответа под резюме чата (вариант B): id резюме hh →
        # роль. Опыт общий (один resume.md), меняется только подача под нишу.
        # Ключ "default" — фолбэк, если id чата нет в карте.
        self._resume_positioning = reply_cfg.get("resume_positioning") or {}

        # Паттерны служебных hh-вставок (виджет отзывов) → их пропускаем при
        # выборе значимого сообщения и глушим (тишина, не эскалация).
        patterns = (
            reply_cfg.get("service_message_patterns") or DEFAULT_SERVICE_PATTERNS
        )
        self._service_patterns = [
            re.compile(p, re.IGNORECASE) for p in patterns
        ]

        self._reply_system = reply_cfg.get("system_prompt") or DEFAULT_REPLY_SYSTEM
        self._reply_instruction = (
            reply_cfg.get("instruction") or DEFAULT_REPLY_INSTRUCTION
        )
        self._external_link_reply = (
            reply_cfg.get("external_link_reply") or DEFAULT_EXTERNAL_LINK_REPLY
        )
        self._test_task_reply = (
            reply_cfg.get("test_task_reply") or DEFAULT_TEST_TASK_REPLY
        )
        self._employment_proof_reply = (
            reply_cfg.get("employment_proof_reply")
            or DEFAULT_EMPLOYMENT_PROOF_REPLY
        )
        self._legal_clearance_reply = (
            reply_cfg.get("legal_clearance_reply") or DEFAULT_LEGAL_CLEARANCE_REPLY
        )

        # Бюджет ответа (Haiku): мало → длинные анкеты обрезаются, JSON не
        # закрывается → ложная эскалация. Для qwen игнорируется (там num_predict).
        self._reply_max_tokens = int(reply_cfg.get("max_tokens", 1024))
        # Концептуальное тестовое (ответ текстом) → Haiku. Режим auto|draft,
        # отдельные бюджет и инструкция (ответ развёрнутый, «2-3 слайда»).
        self._conceptual_mode = (
            reply_cfg.get("conceptual_test_mode") or "auto"
        ).lower()
        self._conceptual_max_tokens = int(
            reply_cfg.get("conceptual_max_tokens", 2048)
        )
        self._conceptual_instruction = (
            reply_cfg.get("conceptual_instruction") or DEFAULT_CONCEPTUAL_INSTRUCTION
        )
        # Параметры многоходового диалога (бот-интервью на route=question).
        self._max_turns = int(reply_cfg.get("max_turns", 5))
        self._poll_timeout = float(reply_cfg.get("poll_timeout_sec", 25))
        self._poll_interval = float(reply_cfg.get("poll_interval_sec", 5))

        ccfg = reply_cfg.get("classifier", {})
        self._classifier = LocalClassifier(
            model=ccfg.get("model", "gemma4:26b"),
            base_url=ccfg.get("base_url", "http://localhost:11434"),
            num_predict=ccfg.get("num_predict", 2048),
            num_ctx=ccfg.get("num_ctx", 8192),
            keep_alive=str(ccfg.get("keep_alive", "30s")),
            think=ccfg.get("think"),
        )
        # Падаем сразу, если Ollama/модель недоступны (по решению — без fallback).
        self._classifier.health_check()
        logger.info(
            "[reply] классификатор: Ollama %s, модель %s",
            self._classifier.base_url,
            self._classifier.model,
        )

    # --- seam overrides ---
    def _should_reply(self, negotiation, last_message) -> bool:
        # Только если последним написал работодатель (без «пинга» непросмотренных).
        if last_message["author"]["participant_type"] != "employer":
            return False
        # Дедуп: это сообщение уже обрабатывали — по id ИЛИ по хэшу текста
        # (бот-эхо шлёт тот же текст с новым id). Тогда не классифицируем повторно.
        prev = self._seen.get(str(negotiation.get("id")))
        if prev:
            if isinstance(prev, dict):
                prev_id, prev_h = prev.get("id"), prev.get("h")
            else:
                prev_id, prev_h = prev, None  # legacy-формат (только id)
            cur_h = _text_hash(last_message.get("text"))
            if last_message.get("id") == prev_id or (prev_h and cur_h == prev_h):
                return False
        return True

    def _ai_reply_enabled(self) -> bool:
        return True  # всегда наш роутинг (classify → action)

    def _generate_reply(
        self, negotiation, vacancy, message_history, placeholders, last_message
    ) -> str:
        msg_text = (last_message.get("text") or "").strip()
        route = self._classify(msg_text, message_history)
        logger.info("[reply] чат %s → route=%s", negotiation.get("id"), route)
        # Для within-chat loop: запоминаем маршрут и id обработанного сообщения.
        self._last_route = route
        self._last_msg_id = last_message.get("id")
        self._last_msg_hash = _text_hash(last_message.get("text"))
        self._cur_cid = str(negotiation.get("id"))  # для дедупа (_mark_seen)
        # Резюме, по которому пришёл этот чат → позиционирование ответа (вариант B).
        self._cur_resume_id = (negotiation.get("resume") or {}).get("id")
        self._cur_resume_title = placeholders.get("resume_title", "")

        if route == "test_task_practical":
            self._review_print(
                "🧪", "test_task_practical", placeholders, msg_text,
                self._test_task_reply,
            )
            return self._test_task_reply

        if route == "test_task_conceptual":
            # Концептуальное тестовое (ответ текстом) → Haiku. auto: ответ в чат;
            # draft: черновик тебе на подтверждение (в чат не шлём).
            if self._haiku is None:
                self._escalate(
                    "test_task_conceptual (Haiku недоступен)",
                    vacancy, placeholders, msg_text,
                )
                return ""
            reply, escalate = self._generate_question_reply(
                self._haiku, vacancy, message_history,
                instruction=self._conceptual_instruction,
                max_tokens=self._conceptual_max_tokens,
            )
            if escalate or not reply:
                self._escalate(
                    "test_task_conceptual→escalate",
                    vacancy, placeholders, msg_text,
                )
                return ""
            if self._conceptual_mode == "draft":
                self._escalate_draft(vacancy, placeholders, msg_text, reply)
                return ""
            self._review_print(
                "🧪", "test_task_conceptual·haiku", placeholders, msg_text, reply
            )
            return reply

        if route == "external_link":
            self._review_print(
                "↪️", "external_link", placeholders, msg_text,
                self._external_link_reply,
            )
            return self._external_link_reply

        if route == "employment_proof":
            self._review_print(
                "📑", "employment_proof", placeholders, msg_text,
                self._employment_proof_reply,
            )
            return self._employment_proof_reply

        if route == "legal_clearance":
            self._review_print(
                "⚖️", "legal_clearance", placeholders, msg_text,
                self._legal_clearance_reply,
            )
            return self._legal_clearance_reply

        if route == "acknowledgement":
            # Авто-подтверждение получения отклика → тишина: не отвечаем и не
            # эскалируем (дёргать тебя по «спасибо, рассмотрим» незачем).
            self._bump("acknowledged")
            self._mark_seen()  # молчим → запомнить, чтобы не гонять каждый прогон
            self._review_print(
                "🤫", "acknowledgement", placeholders, msg_text, None
            )
            return ""

        # document_request — просят прислать документы → решаешь сам (NDA и т.п.).
        if route in ("scheduling", "other", "document_request"):
            self._escalate(route, vacancy, placeholders, msg_text)
            return ""

        # question_simple → локальная модель; question_expertise → Haiku.
        if route in ("question_simple", "question_expertise"):
            if route == "question_expertise":
                chat, chat_label = self._haiku, "haiku"
            else:
                chat = self._chat
                chat_label = f"local:{getattr(self._chat, 'model', '?')}"
            if chat is None:
                # Нужная модель недоступна (нет ключа Haiku / нет Ollama) — не молчим.
                self._escalate(
                    f"{route} (модель недоступна)", vacancy, placeholders, msg_text
                )
                return ""
            reply, escalate = self._generate_question_reply(
                chat, vacancy, message_history
            )
            if escalate or not reply:
                self._escalate(
                    f"{route}→escalate", vacancy, placeholders, msg_text
                )
                return ""
            self._review_print(
                "💬", f"{route}·{chat_label}", placeholders, msg_text, reply
            )
            return reply

        # неизвестный route — на всякий случай эскалируем
        self._escalate(route or "unknown", vacancy, placeholders, msg_text)
        return ""

    # --- helpers ---
    def _is_service_message(self, text: str | None) -> bool:
        """Служебная hh-вставка (виджет отзывов и т.п.) — детект по тексту."""
        t = text or ""
        return any(
            p.search(t) for p in getattr(self, "_service_patterns", ())
        )

    def _pick_significant_message(self, items: list, default):
        """Последнее ЗНАЧИМОЕ сообщение чата (см. базовый seam).

        Логика: среди employer-сообщений, идущих ПОСЛЕ нашего последнего
        сообщения, берём последнее НЕ-служебное. Если таких нет (после нашего
        ответа пришли только служебные вставки) — возвращаем default; его
        _classify распознает как служебное → тишина (а не повторный ответ на
        уже отвеченный вопрос).
        """
        if not items:
            return default
        my_last = -1
        for i, m in enumerate(items):
            if (m.get("author") or {}).get("participant_type") == "applicant":
                my_last = i
        significant = None
        for m in items[my_last + 1:]:
            if (m.get("author") or {}).get("participant_type") != "employer":
                continue
            if self._is_service_message(m.get("text")):
                continue
            significant = m
        return significant or default

    def _classify(self, msg_text: str, message_history: list[str]) -> str:
        # Служебная hh-вставка просочилась как «последнее» (нет реального вопроса
        # после неё) → тишина через acknowledgement, тебя не дёргаем.
        if self._is_service_message(msg_text):
            return "acknowledgement"
        if _FORM_LINK_RE.search(msg_text):
            return "external_link"
        # Мессенджер-контакт с живым рекрутёром → эскалация (route=other), минуя
        # gemma (она ошибочно даёт external_link → авто-отказ). Боты/формы — мимо.
        if _MESSENGER_RE.search(msg_text) and not _MESSENGER_BOT_RE.search(msg_text):
            return "other"
        return self._classifier.classify(msg_text, history=message_history[-6:])

    def _review_print(
        self,
        icon: str,
        label: str,
        placeholders: dict,
        msg_text: str,
        reply: str | None,
    ) -> None:
        """В --review печатает сообщение работодателя И наш ответ (или тишину).

        Без сообщения работодателя по одному ответу не понять, корректен ли он.
        """
        if not self._review:
            return
        emp = placeholders.get("employer_name", "")
        vac = placeholders.get("vacancy_name", "")
        print(f"\n{icon} [{label}] {emp} / {vac}")
        print(f"   📩 Работодатель: {msg_text[:500]}")
        if reply is None:
            print("   (отвечать не требуется)")
        else:
            print(f"   ↩️  Ответ: {reply}")

    # --- дедуп обработанных сообщений ---
    def _load_seen(self) -> dict:
        try:
            return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _mark_seen(self) -> None:
        """Помечает текущее сообщение работодателя обработанным (в памяти).

        Зовём только в «молчаливых» исходах (escalate/acknowledgement), где
        работодатель остаётся последним. Для отвеченных не нужно — наш ответ
        делает последними нас, и _should_reply отсекает чат сам.
        """
        cid = getattr(self, "_cur_cid", None)
        if cid:
            self._seen[cid] = {
                "id": getattr(self, "_last_msg_id", None),
                "h": getattr(self, "_last_msg_hash", None),
            }

    def _save_seen(self) -> None:
        """Сохраняет дедуп-стейт. В --dry-run НЕ пишем — иначе тестовый прогон
        «съест» чаты и боевой их пропустит."""
        if self.dry_run:
            return
        try:
            SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            SEEN_PATH.write_text(
                json.dumps(self._seen, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as ex:
            logger.warning("[reply] не удалось сохранить дедуп-стейт: %s", ex)

    # --- within-chat loop (бот-интервью) ---
    def _after_reply(self, negotiation, nid, vacancy, placeholders) -> None:
        """Многоходовый диалог: продолжаем, пока бот шлёт НОВЫЕ вопросы.

        Только если последний ответ был на question. Стоп по: нет нового
        сообщения в окне, смена маршрута (≠ question), кап ходов, и ЭХО —
        бот повторяет тот же вопрос ИЛИ мы повторяем ответ.
        """
        if not (getattr(self, "_last_route", None) or "").startswith("question"):
            return
        prev_emp_hash = getattr(self, "_last_msg_hash", None)
        prev_reply_norm = None
        for _ in range(self._max_turns - 1):
            result = self._poll_new_employer_message(nid)
            if not result:
                return  # бот молчит / это был человек
            last_message, history = result
            emp_hash = _text_hash(last_message.get("text"))
            # Эхо: бот прислал тот же вопрос (новый id, тот же текст) → стоп +
            # помечаем, чтобы и на след. прогонах не отвечать на этот же текст.
            if emp_hash and emp_hash == prev_emp_hash:
                logger.info("[reply] чат %s — бот повторяет вопрос, стоп петли", nid)
                self._last_msg_id = last_message.get("id")
                self._last_msg_hash = emp_hash
                self._mark_seen()
                return
            logger.info("[reply] чат %s — новое сообщение, ещё ход", nid)
            try:
                text = self._generate_reply(
                    negotiation, vacancy, history, placeholders, last_message
                )
            except AIError as ex:
                logger.warning("[reply] ошибка генерации в петле: %s", ex)
                return
            if not text:
                return  # эскалация/стоп
            # Эхо: наш ответ повторяет предыдущий → дубль не шлём, стоп.
            reply_norm = _norm_text(text)
            if prev_reply_norm is not None and reply_norm == prev_reply_norm:
                logger.info("[reply] чат %s — ответ повторяется, стоп петли", nid)
                self._mark_seen()
                return
            if not self._send_message(nid, text, vacancy):
                return
            if not self._last_route.startswith("question"):
                return  # external_link/test_task: ответили и стоп
            prev_emp_hash = emp_hash
            prev_reply_norm = reply_norm

    def _poll_new_employer_message(self, nid):
        """Ждёт новое сообщение от работодателя (бота). None — не дождались."""
        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            time.sleep(self._poll_interval)
            history, last = self._fetch_messages(nid)
            if (
                last
                and last["author"]["participant_type"] == "employer"
                and last.get("id") != getattr(self, "_last_msg_id", None)
            ):
                return last, history
        return None

    def _fetch_messages(self, nid):
        """Возвращает (история-строки, последнее ЗНАЧИМОЕ сообщение) для чата."""
        page = 0
        last_message = None
        history: list[str] = []
        raw_items: list = []
        while True:
            res = self.api_client.get(
                f"/negotiations/{nid}/messages", page=page
            )
            if not res["items"]:
                break
            raw_items.extend(res["items"])
            last_message = res["items"][-1]
            for m in res["items"]:
                if not m.get("text"):
                    continue
                author = (
                    "Работодатель"
                    if m["author"]["participant_type"] == "employer"
                    else "Я"
                )
                try:
                    d = parse_api_datetime(m.get("created_at")).strftime(
                        "%d.%m.%Y %H:%M:%S"
                    )
                except Exception:
                    d = ""
                history.append(f"[ {d} ] {author}: {m['text']}")
            if page + 1 >= res["pages"]:
                break
            page += 1
        if last_message is not None:
            last_message = self._pick_significant_message(raw_items, last_message)
        return history, last_message

    def _escalate(
        self, route: str, vacancy: dict, placeholders: dict, msg_text: str
    ) -> None:
        self._bump("escalated")
        self._mark_seen()  # не ответили → запомнить, чтобы не эскалировать повторно
        emp = placeholders.get("employer_name", "")
        vac = placeholders.get("vacancy_name", "")
        resume_title = placeholders.get("resume_title", "")
        url = (vacancy or {}).get("alternate_url", "")
        print(f"\n🙋 ТРЕБУЕТ ТЕБЯ [{route}] — {emp} / {vac}")
        if resume_title:
            print(f"   📄 Резюме: {resume_title}")
        print(f"   Сообщение работодателя: {msg_text[:500]}")
        print("   (ответь вручную)")
        # Telegram-уведомление (в dry-run не шлём, чтобы не спамить на тестах).
        if self._notifier and not self.dry_run:
            self._notifier.send(
                f"🙋 Требует тебя [{route}]\n"
                f"🏢 {emp}\n💼 {vac}\n"
                + (f"📄 {resume_title}\n" if resume_title else "")
                + (f"🔗 {url}\n" if url else "")
                + f"\nСообщение работодателя:\n{msg_text[:500]}"
            )

    def _escalate_draft(
        self, vacancy: dict, placeholders: dict, msg_text: str, draft: str
    ) -> None:
        """Концептуальное тестовое в режиме draft: готовый черновик тебе на
        подтверждение, в чат НЕ отправляем (проверяешь и шлёшь сам)."""
        self._bump("escalated")
        self._mark_seen()
        emp = placeholders.get("employer_name", "")
        vac = placeholders.get("vacancy_name", "")
        resume_title = placeholders.get("resume_title", "")
        url = (vacancy or {}).get("alternate_url", "")
        print(f"\n📝 ЧЕРНОВИК НА ПОДТВЕРЖДЕНИЕ [test_task_conceptual] — {emp} / {vac}")
        if resume_title:
            print(f"   📄 Резюме: {resume_title}")
        print(f"   📩 Задание: {msg_text[:500]}")
        print(f"   ✍️  Черновик ответа:\n{draft}")
        print("   (проверь и отправь сам)")
        if self._notifier and not self.dry_run:
            self._notifier.send(
                f"📝 Черновик ответа на тестовое\n"
                f"🏢 {emp}\n💼 {vac}\n"
                + (f"📄 {resume_title}\n" if resume_title else "")
                + (f"🔗 {url}\n" if url else "")
                + f"\nЗадание:\n{msg_text[:500]}\n\nЧерновик:\n{draft}"
            )

    def _send_message(self, nid, message, vacancy) -> bool:
        try:
            ok = super()._send_message(nid, message, vacancy)
        except ApiError as ex:
            # Чат отключён работодателем (вакансия закрыта/архив и т.п.) — не
            # ретраим и не шумим [E]: помечаем обработанным, чтобы дедуп больше
            # не трогал этот чат на следующих прогонах (LLM не тратится).
            if "disabled_by_employer" in str(ex):
                print(f"⏭  Чат отключён работодателем — пропуск (negotiation {nid})")
                self._mark_seen()
                return False
            raise
        if ok:
            self._bump("answered")
        return ok

    def _bump(self, kind: str) -> None:
        """Инкремент счётчика прогона для резюме ТЕКУЩЕГО чата (см. _stats)."""
        title = getattr(self, "_cur_resume_title", "") or "—"
        self._stats[title][kind] += 1

    def _notify_reply_summary(self) -> None:
        total_a = sum(s["answered"] for s in self._stats.values())
        total_e = sum(s["escalated"] for s in self._stats.values())
        total_k = sum(s["acknowledged"] for s in self._stats.values())
        lines = [
            f"📊 reply: ответил {total_a}, эскалировал {total_e}, "
            f"подтверждений без ответа {total_k}."
        ]
        # Разбивка по резюме — чтобы видеть, по какому шла переписка.
        for title, s in self._stats.items():
            lines.append(
                f"📄 {title}: ответил {s['answered']}, "
                f"эскалировал {s['escalated']}, подтв. {s['acknowledged']}"
            )
        msg = "\n".join(lines)
        print(msg)
        if self._notifier and not self.dry_run:
            self._notifier.send(msg)

    def _fetch_full_vacancy(self, vacancy: dict) -> dict:
        vid = vacancy.get("id")
        if not vid:
            return vacancy
        try:
            return {**vacancy, **self.api_client.get(f"/vacancies/{vid}")}
        except Exception as ex:
            logger.warning("[reply] не удалось получить вакансию %s: %s", vid, ex)
            return vacancy

    def _generate_question_reply(
        self, chat, vacancy: dict, message_history: list[str],
        *, instruction: str | None = None, max_tokens: int | None = None,
    ) -> tuple[str, bool]:
        full = self._fetch_full_vacancy(vacancy)
        context = (
            "ПРОФИЛЬ соискателя:\n"
            + _profile_to_text(self._profile)
            + "\n\nРЕЗЮМЕ:\n"
            + self._resume_text.strip()
        )
        system = [
            {
                "type": "text",
                "text": self._reply_system,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": context,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        # Позиционирование под резюме чата (вариант B) — в user, чтобы не ломать
        # кэш resume.md в system-блоке. Опыт общий, меняется только подача роли.
        pos = (
            self._resume_positioning.get(getattr(self, "_cur_resume_id", None))
            or self._resume_positioning.get("default")
            or ""
        )
        pos_line = ""
        if pos:
            title = getattr(self, "_cur_resume_title", "")
            pos_line = (
                f"ПОЗИЦИОНИРОВАНИЕ (отвечай строго под эту роль): {pos}"
                + (f" (резюме на hh: {title})" if title else "")
                + "\n\n"
            )
        user = (
            pos_line
            + f"ВАКАНСИЯ:\n{_vacancy_to_text(full)}\n\n"
            + "ПЕРЕПИСКА:\n" + "\n".join(message_history[-12:]) + "\n\n"
            + f"{instruction or self._reply_instruction}\nВерни JSON."
        )
        raw = chat.complete_with_caching(
            system, user, prefill="{",
            max_tokens=max_tokens or self._reply_max_tokens,
            temperature=0.4, schema=REPLY_SCHEMA,
        )
        data = pick_json(raw, "reply")
        if data is None:
            logger.warning("[reply] не извлёк JSON ответа; raw=%r", raw[:200])
            return "", True  # на всякий — эскалация
        return (data.get("reply") or "").strip(), bool(data.get("escalate"))
