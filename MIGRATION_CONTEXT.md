# Migration Context (handoff)

Этот файл — handoff для AI-агента, который продолжит миграцию в этой папке.
Читай вместе с upstream `README.md` (это форк `s3rgeym/hh-applicant-tool`).

---

## Откуда взялась задача

У соискателя Ян Друбачевский (senior Python/ML backend, см. CV) был свой проект `~/Desktop/hh-auto-apply`, написанный с нуля. После закрытия `GET /vacancies` от hh.ru в апреле 2026 он перестал работать. Решено мигрировать на форк `s3rgeym/hh-applicant-tool`, потому что у того **есть auth через credentials Android-приложения hh** — единственный способ ходить в их API после закрытия.

Это форк `s3rgeym/hh-applicant-tool` в `github.com/ianswings/hh-applicant-tool`. Работаем в ветке `slot-cover-letter-and-yaml-questions`. Upstream добавлен:

```
origin    https://github.com/ianswings/hh-applicant-tool.git
upstream  https://github.com/s3rgeym/hh-applicant-tool.git
```

## Стратегия миграции (Стратегия A — полный форк + наши модули)

Из нашего старого проекта `~/Desktop/hh-auto-apply` переносим **только** уникальные модули, которых нет у s3rgeym:

| Наш модуль | Куда в форке | Что делает |
|---|---|---|
| `src/cover.py` (слот-шаблон) | `our_extensions/cover.py` | Whitelist стека, валидатор, prompt caching, fallback. Главная фича. |
| `src/questions.py` (resolver) | `our_extensions/questions.py` | YAML regex rules → LLM fallback для анкет/тестов. |
| `src/llm_client.py` (Anthropic + прокси) | `ai/anthropic.py` (новый класс по их соглашению) | Будет реализовывать `complete(msg) -> str` (drop-in для их `ChatOpenAI`) **+** наш метод с prompt caching для слот-шаблона. |
| `config.yaml` (whitelist + rules) | `our_extensions/config.yaml` | Их `config.json` остаётся для аккаунта/прокси, наш YAML — для слотов и rules. Два файла, не мержим. |
| `resume.md` | `our_extensions/resume.md` | CV для контекста LLM. |

Старый проект **архивирован** в `~/Desktop/hh-auto-apply-archive`. Текущий `~/Desktop/hh-auto-apply` ещё не удалён, оттуда берём оригиналы модулей.

## Ключевые решения, согласованные с соискателем (НЕ меняй без переспроса)

1. **Cover letter — слот-шаблон, не свободная генерация.** Четыре слота: `greeting`, `stack`, `hook`, `match`. Whitelist жёсткий для `stack`, но в `match` разрешено цитировать стек вакансии дословно (даже если опыта нет — соискатель осознанно взял на себя этот риск).
2. **Greeting:** дефолт «Привет», LLM переключает на «Здравствуйте» при маркерах формальной культуры (Сбер, ВТБ, Газпром, «уважаемые кандидаты»).
3. **LLM:** Claude Haiku 4.5 через нативный `anthropic` SDK с prompt caching. **НЕ через OpenAI-compatible endpoint.**
4. **Прокси:** только для Anthropic (через `ANTHROPIC_PROXY_URL` env). hh.ru ходит напрямую. Их `proxy_url` в конфиге трогает только hh-трафик; для AI используем их же `openai_proxy_url` или нашу независимую логику.
5. **Анкеты:** наш `QuestionResolver` подменяет их `_solve_vacancy_test()`. Логика: regex YAML → LLM fallback → пауза/skip. У них сейчас «середина списка или AI», у нас — намного аккуратнее.
6. **Whitelist стека** в config.yaml расширен под факт `resume.md` (включает Django, SQLAlchemy, MySQL, Pydantic, Nginx, Docker-Compose, OpenAI SDK).

## Hook-точки в их коде (уже найдены)

Файл: `src/hh_applicant_tool/operations/apply_vacancies.py` (61 KB, монолит).

**1. Cover letter** — место присвоения `self.cover_letter_ai` и его использования:
```python
if self.cover_letter_ai:
    msg = self.message_prompt + "\n\n"
    msg += "Название вакансии: " + message_placeholders["vacancy_name"]
    msg += "Мое резюме: " + message_placeholders["resume_title"]
    letter = self.cover_letter_ai.complete(msg)
```
**Проблема:** в `msg` приходит **только** название вакансии + title резюме. Наш слот-шаблон требует полное описание + полный CV. Drop-in `complete(msg)` не подойдёт — придётся **сабклассить их `Operation`** и переопределить этот блок целиком, чтобы достать `vacancy` целиком и наш `resume.md`.

**2. Анкеты** — метод `_solve_vacancy_test()` (~785-850). Структура task:
- `task["description"]` — текст вопроса
- `task["candidateSolutions"]` — список вариантов `{"id": ..., "text": ...}` (если есть → multiple choice; иначе → открытый вопрос)

Их текущая логика: для MC — берёт середину списка или AI выбирает ID; для открытых — `"Да"` или AI с промптом `f"Дай краткий и профессиональный ответ на вопрос: {question}"`.

Наша замена: сабкласс с переопределением `_solve_vacancy_test()`, внутри — наш `QuestionResolver` + матчинг текста ответа на solution_id для MC.

**3. Поиск** — `_get_vacancies()`. **Не трогаем**, работает через их API с Android-токеном.

## Их архитектура — что важно знать

- **Плагины:** модули в `src/hh_applicant_tool/operations/` автоматически становятся CLI-командами. Наследуются от `BaseOperation` (см. `main.py`). Шаблон — `operations/whoami.py`.
- **Базовый класс плагина:** `BaseOperation` имеет методы `setup_parser(parser)` и `run(tool, args)`. Доступ к `tool.api_client`, `tool.storage.settings`, `tool.session`, `tool.xsrf_token`, `tool.config`.
- **AI:** `src/hh_applicant_tool/ai/` — `base.py` (только `AIError`), `openai.py` (`ChatOpenAI` с `complete(msg)`). Импортируется через `ai/__init__.py`. Туда же положим `anthropic.py`.
- **Storage:** `~/.config/hh-applicant-tool/config.json` + SQLite.
- **Auth:** команда `authorize` — открывает hh.ru через Playwright, получает OAuth-токен с Android client_id. Соискатель согласен пройти этот шаг.
- **Зависимости:** Python **3.11+** (соискателю нужно поставить, у него сейчас 3.10), **Poetry**.

## План работ

### Этап 0 — окружение (на стороне соискателя)
1. `brew install python@3.11`
2. `brew install poetry`
3. `cd ~/Desktop/hh-applicant-tool && poetry install --extras playwright`
4. `poetry run hh-applicant-tool authorize` — пройти их auth-флоу, получить токен.

### Этап 1 — перенос модулей (агент делает в этой папке)
1. **`src/hh_applicant_tool/ai/anthropic.py`** — новый класс `ChatAnthropic`:
   - Метод `complete(msg: str) -> str` (drop-in для `ChatOpenAI`).
   - Метод `complete_with_caching(system: list, user: str) -> str` — для нашего слот-шаблона, с `cache_control: ephemeral` на резюме+правилах.
   - SOCKS5/HTTP прокси через `httpx.Client(proxy=...)` (env `ANTHROPIC_PROXY_URL`).
   - Модель по умолчанию: `claude-haiku-4-5`.
   - Экспорт из `ai/__init__.py`.

2. **`our_extensions/cover.py`** — портируем из `~/Desktop/hh-auto-apply/src/cover.py`. Главная функция `build_cover_letter(vacancy: dict, resume_text: str, chat: ChatAnthropic, cfg: dict) -> str`. Внутри: формирование slot JSON через `chat.complete_with_caching(...)`, валидация (whitelist, длины), fallback на дефолтный текст.

3. **`our_extensions/questions.py`** — портируем `QuestionResolver`:
   - `resolve(question: str, options: list[(id, text)] | None) -> str | None`
   - Для MC: после получения ответа находим solution_id по совпадению текста.
   - LLM-фолбэк через `ChatAnthropic.complete(prompt)`.

4. **`our_extensions/config.yaml`** — портируем наш YAML (слоты, whitelist, rules). Утилита `load_config()` в `our_extensions/__init__.py` или отдельном `config_loader.py`.

5. **`our_extensions/resume.md`** — копируем CV из `~/Desktop/hh-auto-apply/resume.md`.

### Этап 2 — врезка (новый Operation)
Создать `src/hh_applicant_tool/operations/apply_slot.py`:
- Сабкласс их `Operation` из `apply_vacancies.py`.
- Переопределяет блок формирования `letter`: вызывает наш `build_cover_letter(vacancy, resume_text, chat, cfg)`.
- Переопределяет `_solve_vacancy_test()`: внутри строит payload как у них, но answers получает через `QuestionResolver`.
- CLI-имя команды — `apply-slot` (либо `apply` если хотим сразу заменить — обсуждается).

### Этап 3 — конфиг и проверка
1. Передать в `apply_slot` пути к `our_extensions/config.yaml` и `our_extensions/resume.md` через args или фиксированно.
2. Прогон `--dry-run` на 3-5 вакансиях.
3. `--review` (если их система это поддерживает; иначе добавить).
4. Обновить `AGENTS.md` форка (создать заново, описать что было сделано).
5. Обновить `README.md` форка (либо нашу секцию вписать, либо отдельный `README_SLOT.md`).

### Этап 4 — ежедневный workflow
- Прокси для Anthropic через `docker compose up -d` (docker-compose.yml из старого проекта, перенести сюда).
- `.env` с `ANTHROPIC_API_KEY`, `ANTHROPIC_PROXY_URL`, `BLANCVPN_OUTLINE_KEY`.
- Запуск: `poetry run hh-applicant-tool apply-slot`.

## Что НЕ делать (известные грабли)

- **Не использовать OpenAI-compatible endpoint Anthropic** — теряем prompt caching, который даёт экономию в 6-10x.
- **Не пытаться вернуть наш search через requests без токена** — закрыто, 403. Только через их API client.
- **Не сливать наш YAML в их JSON** — два разных конфига, два разных назначения.
- **Не трогать `_get_vacancies()` и auth** — это их core, работает.
- **Не делать proxy_url в их config.json для AI** — у нас selective прокси через httpx, hh должен ходить напрямую.
- **Не менять greeting policy** без согласования.
- **Whitelist стека**: добавление технологий вне `resume.md` — без согласования.

## Состояние на момент handoff

- ✅ Форк создан в `github.com/ianswings/hh-applicant-tool`.
- ✅ Клонирован в `~/Desktop/hh-applicant-tool`.
- ✅ Ветка `slot-cover-letter-and-yaml-questions` создана и активна.
- ✅ Upstream добавлен.
- ✅ Папка `src/hh_applicant_tool/our_extensions/` создана с пустым `__init__.py`.
- ⏳ Соискатель ставит Python 3.11 + Poetry.
- ⏳ Сразу следующий шаг: написать `src/hh_applicant_tool/ai/anthropic.py` (см. Этап 1 пункт 1).

## Оригиналы наших модулей (откуда копируем)

```
~/Desktop/hh-auto-apply/src/cover.py        → our_extensions/cover.py (с адаптацией интерфейса)
~/Desktop/hh-auto-apply/src/questions.py    → our_extensions/questions.py (с адаптацией под task["candidateSolutions"])
~/Desktop/hh-auto-apply/src/llm_client.py   → ai/anthropic.py (расширить до полноценного ChatAnthropic)
~/Desktop/hh-auto-apply/config.yaml         → our_extensions/config.yaml
~/Desktop/hh-auto-apply/resume.md           → our_extensions/resume.md
~/Desktop/hh-auto-apply/.env.example        → .env.example (в корень)
~/Desktop/hh-auto-apply/docker-compose.yml  → docker-compose-anthropic.yml (их docker-compose.yml уже есть, переименуем наш)
```

## Профиль соискателя (для LLM-контекста)

- senior Python backend, 6+ лет коммерческого опыта
- стек: см. `resume.md`
- ЗП ожидание: от 400к на руки
- готов к удалёнке/гибриду, не готов к командировкам и релокации
- английский C1
- gражданство РФ, Москва
- email для UA: `iancodex@yandex.ru`

## Контакты на момент handoff

Соискатель сейчас параллельно: ставит Python 3.11 + Poetry, может уже прошёл `authorize`. Спроси статус, не дублируй то, что он уже сделал.
