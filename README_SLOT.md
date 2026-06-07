# apply-slot — слот-шаблон cover letter + YAML-резолвер анкет

Наше расширение поверх форка `s3rgeym/hh-applicant-tool`. Добавляет CLI-команду
`apply-slot` — то же, что их `apply`, но:

- **Сопроводительное письмо** собирается по **слот-шаблону** (не свободная
  генерация), через Claude Haiku 4.5 с **prompt caching**. Четыре слота:
  `greeting`, `stack` (жёсткий whitelist), `hook`, `match`.
- **Анкеты/тесты** решаются нашим `QuestionResolver`: сначала regex-правила из
  YAML, затем LLM-фолбэк, затем дефолт базового класса.

Поиск, отклик, капча, фильтры — наследуются от их `Operation` без изменений.

## Архитектура

```
src/hh_applicant_tool/
├── ai/anthropic.py            # ChatAnthropic: complete() + complete_with_caching()
├── our_extensions/
│   ├── __init__.py            # load_config(), load_resume()
│   ├── config.yaml            # слоты + whitelist + rules анкет (НАШ конфиг)
│   ├── resume.md              # CV для контекста LLM
│   ├── cover.py               # build_cover_letter() — слот-шаблон + валидация
│   └── questions.py           # QuestionResolver — regex → LLM
└── operations/
    ├── apply_vacancies.py     # их монолит; вынесены два seam-метода
    └── apply_slot.py          # НАШ сабкласс: переопределяет seam'ы
```

`apply_slot.Operation` сабклассит `apply_vacancies.Operation` и переопределяет
ровно два «шва», вынесенных в базовом классе:

| Seam | База (apply) | apply-slot |
|---|---|---|
| `_generate_letter(vacancy, ph)` | свободный AI / `cover_letter % ph` | `build_cover_letter()` — слот-шаблон |
| `_solve_test_task(task)` | середина списка / «Да» | `QuestionResolver` (regex → LLM) |
| `_recognize_captcha(img)` | OpenAI-vision (`openai_captcha`) | Anthropic vision (`ChatAnthropic.solve_captcha`), fallback на OpenAI |

Два конфига **не смешиваются**: аккаунт/прокси hh.ru — в их
`~/.config/hh-applicant-tool/config.json`; слоты и rules — в нашем
`our_extensions/config.yaml`.

**Капча.** Капча *во время откликов* распознаётся через Anthropic vision (та же
модель `llm.model`, через `ANTHROPIC_PROXY_URL`). Если Anthropic-ключа нет —
откат на их OpenAI-vision (секция `openai_captcha` в `config.json`). Капча *при
`authorize`* остаётся ручной (вводишь сам через `--kitty`/`--sixel`) — этот путь
мы не меняли.

## Установка

```bash
brew install poetry            # python 3.11+ (3.12 тоже подходит)
cd hh-applicant-tool
poetry install --extras playwright
poetry run hh-applicant-tool authorize   # OAuth через Android client_id hh
```

## Окружение

Anthropic-ключ и прокси берутся из переменных окружения (см. `.env.example`):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_PROXY_URL=socks5://127.0.0.1:1080   # опционально, нужен из РФ
```

Если `ANTHROPIC_API_KEY` не задан — письма идут по нейтральному fallback,
а анкеты решаются только regex-правилами (LLM-фолбэк выключается).

## Запуск

```bash
# Превью без отправки — посмотреть качество писем и ответов на 3-5 вакансиях:
poetry run hh-applicant-tool apply-slot --search "Python" --review --dry-run --total-pages 1

# Боевой запуск:
poetry run hh-applicant-tool apply-slot --search "Python"
```

Полезные флаги (наследуются от `apply`): `--search`, `--resume-id`,
`--total-pages`, `--per-page`, `--force-message`, `--skip-tests`, `--dry-run`.

Наши флаги:

| Флаг | Назначение |
|---|---|
| `--review` | Печатать сгенерированное письмо и ответы на анкету. Совмещай с `--dry-run`. |
| `--our-config PATH` | Свой `config.yaml` (по умолчанию `our_extensions/config.yaml`). |
| `--our-resume PATH` | Свой `resume.md` (по умолчанию `our_extensions/resume.md`). |

## Ежедневный workflow (selective-прокси)

Anthropic блокирует РФ-IP, поэтому AI-трафик идёт через SOCKS5-прокси, а **hh.ru —
напрямую** с российского IP. Прокси поднимается отдельным docker-compose:

```bash
cp .env.example .env        # заполни ANTHROPIC_API_KEY и BLANCVPN_OUTLINE_KEY
./run-apply-slot.sh --review --dry-run --total-pages 1   # тест без отправки
./run-apply-slot.sh                                       # боевой запуск
```

Наш поисковый запрос (язык запросов hh) и параметры (`--area 113`,
`--order-by publication_time`, `--per-page 100`, `--total-pages 5`) **зашиты в
скрипте** в массиве `SEARCH_ARGS` — отдельно передавать `--search` не нужно.
Любой флаг можно переопределить, дописав его при вызове (он добавляется после и
перебивает дефолт). Там же закомментирован `--no-magic` — включи после теста,
если «умный» поиск hh искажает выдачу по query-language запросу.

`run-apply-slot.sh`:
1. грузит `.env`;
2. **вычищает `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`** — чтобы requests-сессия hh
   их не подхватила (это единственный канал, через который hh мог бы случайно
   уйти в прокси);
3. выбирает прокси по `ANTHROPIC_PROXY_MODE`:
   - `external` (по умолчанию) — Docker не трогает, берёт `ANTHROPIC_PROXY_URL`
     из `.env` как есть (внешний прокси; пусто = напрямую);
   - `blancvpn` — поднимает `docker-compose-anthropic.yml` (sslocal) и форсит
     `ANTHROPIC_PROXY_URL=socks5://127.0.0.1:1080`;
4. запускает `apply-slot` с зашитым запросом + твоими доп. аргументами.

### Почему hh точно ходит напрямую

`ANTHROPIC_PROXY_URL` читается **только** в `ai/anthropic.py` (через отдельный
`httpx.Client`). hh-сессия в `main.py` берёт прокси исключительно из `--proxy-url`
/ `config.json::proxy_url` / `HTTP_PROXY`. Ни одно из них не задаём → hh direct.

Проверка (прокси поднят):

```bash
# Anthropic-маршрут (через прокси) — не-РФ IP:
curl -s --socks5 127.0.0.1:1080 https://api.ipify.org; echo
# hh-маршрут (напрямую) — твой РФ IP:
curl -s https://api.ipify.org; echo
```

Прокси можно оставить поднятым (`restart: unless-stopped`); остановить —
`docker compose -f docker-compose-anthropic.yml down`.

## Конфиг `our_extensions/config.yaml`

- `profile` — **единый источник** фактов о соискателе (ЗП, формат, английский,
  гражданство). Читается и письмом (для ответов на вопросы вакансии), и
  резолвером анкет. Меняешь тут — меняется везде.
- `cover_letter.template` — шаблон письма с плейсхолдерами `{{greeting}}`,
  `{{stack}}`, `{{hook}}`, `{{match}}`. Второй абзац (агентная разработка) —
  **статика**, гарантированно в каждом письме.
- `cover_letter.stack_whitelist` — единственный источник технологий для слота
  `stack`. Расширять только под факт `resume.md`.
- `cover_letter.default_stack` — стек для fallback-письма.
- `questions.rules` — список `{match: <regex>, answer: <str>}`; `answer: __llm__`
  принудительно отдаёт вопрос LLM.
- `questions.fallback` — `llm | pause | skip`, если ни одно правило не сматчилось.
- `llm.model | max_tokens | temperature` — параметры Anthropic.

**Кодовые слова в вакансии.** Если вакансия требует вставить слово/ответить на
вопрос в сопроводительном («напишите слово X», «ответьте, готовы ли вы к…»),
LLM извлекает это в поле `compliance`: слово ставится строкой в начало письма,
ответ — в `P.S.`. Кодовое слово проверяется на наличие в тексте вакансии
(анти-галлюцинация). Чтобы это срабатывало везде, `run-apply-slot.sh` шлёт
письмо на **каждую** вакансию (`--force-message`).

**Офис/гибрид.** Если вакансия требует офис или гибрид (поля `schedule`/
`work_format` или явное упоминание в тексте), в письмо добавляется строка
`cover_letter.remote_note` — про предпочтение полной удалёнки. Срабатывает по
двойному сигналу: LLM-флаг `requires_onsite` **и** regex-гард по тексту вакансии.
Для remote-вакансий не появляется.

## reply-slot — переписка с работодателями

Команда `reply-slot` отвечает в чатах hh, где **последним написал работодатель**.
Каждое сообщение классифицируется **локально** (Ollama + Gemma), затем маршрут:

| Класс | Действие |
|---|---|
| `external_link` (форма/бот по ссылке) | авто фикс-отказ (`reply.external_link_reply`) |
| `scheduling` (время/собес) | **эскалация тебе** (в LLM не шлётся, решаешь сам) |
| `question` (вопросы, в т.ч. бот) | **авто-ответ** через Anthropic (profile + resume + вакансия, кэш) |
| `other` / неясно | эскалация тебе |

Anthropic-ответ под жёсткими гардами (`reply.system_prompt`): только удалёнка,
ЗП из `profile`, без твёрдых обещаний; если LLM не уверен — ставит `escalate` и
ответ не отправляется, а помечается тебе.

**Требует Ollama** (см. `reply.classifier`):
```bash
brew install ollama
ollama pull gemma4:26b        # или gemma4:e4b, если памяти мало → правишь model в config.yaml
```
Если Ollama недоступен — `reply-slot` **падает** с понятной ошибкой (классификатор обязателен).

Запуск (selective-прокси как у apply; классификация — локально):
```bash
./run-reply-slot.sh --review --dry-run    # превью: класс + ответ/эскалация, без отправки
./run-reply-slot.sh                         # боевой (вопросы отвечаются авто)
```

## Ключевые решения (не менять без согласования)

1. Cover letter — слот-шаблон, не свободная генерация. В `match` разрешено
   цитировать стек вакансии дословно даже без опыта (осознанный риск соискателя).
2. Greeting: дефолт «Привет», LLM переключает на «Здравствуйте» при маркерах
   формальной культуры (банк/госкомпания).
3. LLM: только нативный `anthropic` SDK с prompt caching — НЕ OpenAI-endpoint.
4. Прокси: только для Anthropic (`ANTHROPIC_PROXY_URL`), hh.ru ходит напрямую.
