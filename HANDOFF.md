# HANDOFF — текущее состояние (для нового чата)

Это сводка для AI-агента, продолжающего работу. Читать вместе с:
- `AGENTS.md` — что сделано по этапам (1–8), грабли, как проверять;
- `MIGRATION_CONTEXT.md` — исходный контекст миграции и согласованные решения;
- `README_SLOT.md` — пользовательская инструкция (apply-slot/reply-slot, конфиг);
- `launchd/README.md` — автозапуск.

Форк `s3rgeym/hh-applicant-tool`, ветка `slot-cover-letter-and-yaml-questions`.
Соискатель: senior Python backend, удалёнка-only, ЗП от 400к, см. `resume.md`.

## Архитектура (что где крутится)
- **apply-slot** (`operations/apply_slot.py`) — отклики: слот-письмо (`our_extensions/cover.py`),
  анкеты (`our_extensions/questions.py`), капча.
- **reply-slot** (`operations/reply_slot.py`) — переписка: классификация (локально) →
  роутинг (фикс-ответы / эскалация / Anthropic-ответ), within-chat loop для бот-вопросов.
- **LLM-провайдер переключаемый** (`ai/factory.py::make_chat`, `llm.provider`):
  - **сейчас `local`** — `OllamaChat` (`ai/local.py`), модель **`gemma4:e4b`** через Ollama,
    для писем/анкет/reply-ответов/классификации;
  - **капча — гибрид**: всегда `ChatAnthropic` (`ai/anthropic.py`) через прокси (отдельный
    `_captcha_chat` в apply_slot). Anthropic-путь рабочий, включается `llm.provider: anthropic`.
- **Прокси** — общий `PROXY_URL` (читают и Anthropic, и Telegram). Сейчас это
  **SSH-SOCKS туннель к VPS Time4VPS** `89.47.164.209` (Литва) через **autossh**
  (launchd `com.hh.proxy`, `~/.ssh/hh_vps`). hh.ru ходит НАПРЯМУЮ.
- **Telegram** (`our_extensions/notify.py`) — эскалации reply + отчёты apply/reply,
  через `PROXY_URL`. Секреты в `.env` (`TELEGRAM_BOT_TOKEN/CHAT_ID`).
- **Автозапуск** — launchd: `com.hh.apply` (:00), `com.hh.reply` (:30), `com.hh.proxy`
  (постоянный туннель). Общий PID-lock `/tmp/hh-tool.lock` (`lock.sh`): мёртвый снимаем,
  зависший >3ч убиваем/замещаем, живой долгий — пропускаем.
- **Дневной лимит**: apply возвращает exit-code 10 (лимит после отправок) / 11 (лимит при
  0 отправок) / 0; `run-apply-slot.sh` пишет паузу `.state/apply_pause_until` (+24ч5м).

## Память / Ollama (машина 24GB)
- `gemma4:e4b` (~5GB). **`gemma4:26b` НЕ влезает** с Chrome/VSCode (своп).
- `num_ctx: 12288` (письма) / `8192` (классификатор) — иначе дефолт gemma4 256K раздувает RAM.
- `keep_alive: 30s` — выгрузка в простое, без перезагрузок внутри прогона.

## ⚠️ ОТКРЫТЫЙ ВОПРОС №1 — надёжность structured output на e4b
Баг Ollama [#15260](https://github.com/ollama/ollama/issues/15260): `think`+`format` на gemma4 ломается
(`think:false`→format игнорится; `think:true`→reasoning течёт в content как `{"reasoning":...}`).
**Что сделано:** при `format` НЕ шлём `think` (омит); добавлен устойчивый парсер `pick_json`
(берёт нужный JSON-объект из нескольких), срез ```-фенсов, `strict=False`, паддинг стека из whitelist.
**Статус:** ждём проверку dry-run, что письма идут БЕЗ fallback. Если e4b всё равно часто мажет —
**переключить `llm.local_model` и `reply.classifier.model` на `qwen2.5:7b-instruct`** (не-thinking →
бага нет, надёжный JSON, ~5GB). Это план Б.

## Запуск
```bash
# dry-run превью (без отправки):
./run-apply-slot.sh --review --dry-run --per-page 5 --total-pages 1
./run-reply-slot.sh --review --dry-run
# боевой:
./run-apply-slot.sh          # отклики
./run-reply-slot.sh          # переписка
# автозапуск: см. launchd/README.md
```
Предусловия: `poetry install --extras playwright`; пройден `authorize` (токен в
`~/.config/hh-applicant-tool/`); Ollama поднята + `ollama pull gemma4:e4b`; `.env` заполнен;
autossh-туннель жив (`curl --socks5 127.0.0.1:1080 https://api.ipify.org` → не-РФ IP).

## Не менять без согласования с соискателем
Слот-шаблон письма; greeting-policy (Привет/Здравствуйте); whitelist стека; тексты фикс-ответов
(тестовое/ссылки/ТК/удалёнка); гибрид капчи; «hh напрямую, прокси только для AI». Тексты и правила —
в `our_extensions/config.yaml`.

## Незакрытые задачи
1. Подтвердить, что после фикса парсера письма e4b идут без fallback (или перейти на qwen2.5:7b).
2. Закончить верификацию боевого запуска (один ручной прогон → включить launchd).
3. Проект перенесён из `~/Desktop` в `~/hh-applicant-tool` (TCC: launchd не читает Desktop).
