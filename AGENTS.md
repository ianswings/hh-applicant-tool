# AGENTS.md

Заметки для AI-агента, работающего в этом форке. Полный handoff-контекст —
в `MIGRATION_CONTEXT.md`. Пользовательская документация — в `README_SLOT.md`.
Этот файл — про то, что уже сделано и как тут устроены наши правки.

## Что это

Форк `s3rgeym/hh-applicant-tool` с нашим расширением: команда `apply-slot`
(слот-шаблон cover letter через Anthropic + YAML-резолвер анкет). Зачем форк, а
не свой проект: после закрытия `GET /vacancies` в апреле 2026 ходить в API hh.ru
можно только с OAuth-токеном Android-приложения, который даёт их `authorize`.

Ветка: `slot-cover-letter-and-yaml-questions`.

## Сделано (Этапы 1–3)

### Этап 1 — модули
- `src/hh_applicant_tool/ai/anthropic.py` — `ChatAnthropic`:
  `complete(msg)` (drop-in для их `ChatOpenAI`) и
  `complete_with_caching(system, user, prefill=...)` с `cache_control: ephemeral`.
  Прокси через `httpx`/`ANTHROPIC_PROXY_URL`, модель по умолчанию `claude-haiku-4-5`,
  фабрика `from_env()`. Экспортирован из `ai/__init__.py`.
- `src/hh_applicant_tool/our_extensions/cover.py` —
  `build_cover_letter(vacancy, resume_text, chat, cfg)`: слоты через LLM,
  валидация (whitelist, длины greeting/stack/hook/match), fallback на нейтральное
  письмо при ошибке/невалидности.
- `src/hh_applicant_tool/our_extensions/questions.py` — `QuestionResolver`:
  `resolve(question, vacancy_text, options)` (regex → LLM) и
  `pick_solution_id(question, solutions, vacancy_text)` для multiple-choice
  (матчит текст ответа обратно на `candidateSolutions[].id`).
- `our_extensions/config.yaml` (только наши секции: `cover_letter`, `questions`,
  `llm`) + `resume.md`. Загрузка — `load_config()` / `load_resume()` в
  `our_extensions/__init__.py`.

### Этап 2 — врезка через seam'ы
В `operations/apply_vacancies.py` сделан **поведение-сохраняющий** extract-method:
- `_generate_letter(vacancy, message_placeholders) -> str` — вынесен inline-блок
  генерации письма из `_apply_resume`.
- `_solve_test_task(task) -> (key, value)` — вынесена логика одного задания из
  цикла `_solve_vacancy_test`.
- `_solve_vacancy_test` получил опциональный `vacancy=` и пишет его в
  `self._current_vacancy` (контекст для сабкласса).
- `_recognize_captcha(img_bytes) -> str` — вынесен вызов AI-распознавания капчи
  из `_solve_captcha_async`. База зовёт их `get_captcha_ai().solve_captcha`;
  `apply_slot` переопределяет на `ChatAnthropic.solve_captcha` (vision), с
  fallback на OpenAI при отсутствии Anthropic-ключа. `authorize`-капча ручная,
  не затронута.

`operations/apply_slot.py` — сабкласс `apply_vacancies.Operation`, переопределяет
оба seam'а (см. таблицу в `README_SLOT.md`). `__aliases__ = []`, чтобы не
конфликтовать с их `apply`/`apply-similar`. Добавляет аргументы `--our-config`,
`--our-resume`, `--review`.

### Этап 3 — конфиг, превью, доки
- Пути конфига/резюме — через `--our-config` / `--our-resume` (дефолты в
  `our_extensions/`).
- `--review` — печать письма и ответов перед отправкой (совмещать с `--dry-run`).
- `README_SLOT.md`, этот `AGENTS.md`, `.env.example`.
- В `pyproject.toml` добавлены `anthropic` и `pyyaml`.

**Не сделано (требует тебя/окружения):** живой `--dry-run` прогон — упирается в
интерактивный `authorize` (нет `~/.config/hh-applicant-tool/`) и реальный аккаунт
hh.ru. Это Этап 3 п.2; выполняется только на стороне соискателя.

## Грабли (не повторяй)

- **Не сливать наш YAML в их `config.json`** — два разных конфига, два назначения.
- **Не трогать `_get_vacancies()` и `authorize`** — это их core, работает.
- **Только нативный `anthropic` SDK** — OpenAI-compatible endpoint теряет prompt
  caching (экономия 6-10x).
- **Прокси — только для Anthropic** (`ANTHROPIC_PROXY_URL`), hh.ru напрямую.
- **Whitelist стека** расширять только под факт `resume.md`; greeting-policy и
  rules анкет — не менять без согласования с соискателем.
- Анкеты с тестом тянут дополнительный `GET /vacancies/{id}` в `_generate_letter`
  для полного описания; при `--ai-filter heavy` описание тянется дважды
  (потенциальная оптимизация — кэш full-vacancy на итерацию).

## Как проверять без сети

Импорты ленивые (anthropic SDK и yaml подгружаются только при вызове), поэтому
seam-методы и резолвер тестируются офлайн с заглушкой `api_client` и `chat=None`.
Парсер собирается через `HHApplicantTool()._parser`. Целевой рантайм — Python
3.11+ (3.12 подходит); системный 3.9 не годится (`KW_ONLY` из dataclasses 3.10+).

### Этап 4 — ежедневный workflow (selective-прокси)
- `docker-compose-anthropic.yml` — sslocal SOCKS5 на `127.0.0.1:1080` (их
  собственный `docker-compose.yml` не трогали).
- `run-apply-slot.sh` — грузит `.env`, **вычищает `HTTP_PROXY`/`HTTPS_PROXY`/
  `ALL_PROXY`** (иначе hh-сессия подхватила бы их), поднимает прокси, запускает
  `apply-slot`.
- Изоляция прокси подтверждена: `ANTHROPIC_PROXY_URL` читается ТОЛЬКО в
  `ai/anthropic.py`; hh берёт прокси лишь из `--proxy-url` / `config.json` /
  `HTTP_PROXY` — ничего из этого не задаём → hh direct. См. раздел проверки в
  `README_SLOT.md`.

Осталось на тебе (требует сети/аккаунта): пройти `authorize`, заполнить `.env`,
прогнать `--review --dry-run`, затем боевой запуск.

### Этап 5 — переписка с работодателями (reply-slot)
- Seam в `reply_employers.py`: `run`→`_setup`+`reply_employers`, плюс
  `_should_reply` / `_ai_reply_enabled` / `_generate_reply` + гард «пусто → не
  шлём». Поведение их `reply` сохранено.
- `ai/local.py` — `LocalClassifier` (Ollama HTTP, JSON-schema enum, `health_check`).
  Если Ollama/модель недоступны — `ClassifierError` (падаем, без regex-fallback).
- `operations/reply_slot.py` — сабкласс: триггер «работодатель последним»;
  классификация локально → роутинг: `external_link`→фикс-отказ, `scheduling`/
  `other`→эскалация (в LLM не шлём), `question`→Anthropic-ответ (profile+resume+
  вакансия, кэш, гарды, флаг `escalate`). `__aliases__=[]`.
- `config.yaml` → секция `reply` (classifier model `gemma4:26b`, system_prompt,
  instruction, external_link_reply).
- `run-reply-slot.sh` — как apply, + проверка, что Ollama поднят.
- Классификация локальная (Ollama, не зависит от прокси); ответ — Anthropic через
  прокси; hh — напрямую.
- **test_task** — отдельный класс (приоритет выше external_link) + в письме
  `requires_test_task`+`_TESTTASK_RE`→`cover_letter.test_task_note`. Тексты:
  `reply.test_task_reply` / `cover_letter.test_task_note`.
- **within-chat loop** (бот-интервью): `_after_reply` в базе (hook) + в reply_slot
  поллинг новых сообщений; крутится только на `question`, кап `reply.max_turns`,
  `poll_timeout_sec`, `poll_interval_sec`; стоп при смене маршрута/молчании/капе.
- Thinking-модели Gemma 4 требуют большой `num_predict` (мышление + ответ): в
  `LocalClassifier` 2048; иначе пустой `content`. `think` настраивается в конфиге.

### Этап 6 — автоматизация (launchd, ежечасно)
- `launchd/com.hh.apply.plist` (:00) и `com.hh.reply.plist` (:30) + `launchd/README.md`.
- **Общий lock** `/tmp/hh-tool.lock` в обоих скриптах → apply и reply не идут
  одновременно (один hh-токен).
- **exit-code из apply** (`apply_vacancies.run`): `10` — дневной лимит ПОСЛЕ
  отправок, `11` — лимит при 0 отправок, `0` — норма. `main` пробрасывает код.
- `run-apply-slot.sh`: пауза-стейт `.state/apply_pause_until`. На `10` — пауза
  +24ч5м (до завтра); на `11` — без паузы (повтор каждый час, лимит не сбросился);
  пока пауза активна — apply пропускается, reply работает.
- `logs/`, `.state/` — в `.gitignore`.

### Этап 7 — переключаемый провайдер LLM (local/Anthropic), гибрид капчи
- `ai/local.py::OllamaChat` — drop-in для `ChatAnthropic` (те же `complete`,
  `complete_with_caching`, `solve_captcha`); ошибки оборачивает в `AnthropicError`.
  `format:json` для JSON-вызовов; `num_predict` большой (thinking); `think` из конфига.
- `ai/factory.py::make_chat(llm_cfg)` — по `llm.provider` (`local`|`anthropic`).
- `config.yaml::llm`: `provider: local`, `local_model: gemma4:e4b`, `num_predict: 4096`,
  `think: true`; поля `model/max_tokens/temperature` остаются для Anthropic.
- apply/reply: `self._chat = make_chat(...)` + `health_check()` (падаем, если Ollama нет).
- **Капча — гибрид:** `apply_slot._captcha_chat = ChatAnthropic.from_env(...)`,
  `_recognize_captcha` идёт в него (не в local). Значит ANTHROPIC_KEY + прокси нужны
  ТОЛЬКО для капчи (редко); основной путь — локальный, без прокси.
- Классификатор reply тоже `gemma4:e4b`.
- Переключение обратно на облако: `llm.provider: anthropic` (одна строка).

### Этап 8 — Telegram-уведомления + общий прокси
- `our_extensions/notify.py::TelegramNotifier` (`from_env`: TELEGRAM_BOT_TOKEN/CHAT_ID),
  шлёт через **PROXY_URL** (Telegram из РФ режется); ошибки глотает.
- **Общая переменная прокси `PROXY_URL`** (старое `ANTHROPIC_PROXY_URL` читается как
  fallback): её используют и `ChatAnthropic` (капча), и `TelegramNotifier`. В скриптах
  режим — `PROXY_MODE` (fallback `ANTHROPIC_PROXY_MODE`).
- reply: эскалации (`document_request`/`scheduling`/`other`/`question→escalate`) шлются
  в Telegram + отчёт «ответил N, эскалировал M». apply: отчёт «отправлено N; лимит».
- В `--dry-run` Telegram не шлётся (только print).
