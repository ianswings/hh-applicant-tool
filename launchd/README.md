# Автоматизация через launchd

Агенты:
- **com.hh.apply** в `:00` (раз в час) — отклики (`run-apply-slot.sh`);
- **com.hh.reply** в `:30` (раз в час) — переписка в чатах (`run-reply-slot.sh`);
- **com.hh.update** каждые 4:00–4:30 — подъём резюме в поиске (`run-update-slot.sh`).

Сдвиг :00/:30 + **общий lock** (`/tmp/hh-tool.lock`) гарантируют, что apply и reply
**не идут одновременно** (используют один hh-токен — параллелить нельзя).

**update НЕЗАВИСИМ** от apply/reply: подъём — это один короткий
`POST /resumes/{id}/publish`, не длинный прогон, поэтому он не берёт общий lock и
**может идти параллельно** с apply/reply. Свой lock (`/tmp/hh-update.lock`) защищает
лишь от наложения двух update. Ему не нужны `.env`/Anthropic/Ollama/прокси.

## Установка
```zsh
cp launchd/com.hh.apply.plist ~/Library/LaunchAgents/
cp launchd/com.hh.reply.plist ~/Library/LaunchAgents/
cp launchd/com.hh.update.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.hh.apply.plist
launchctl load -w ~/Library/LaunchAgents/com.hh.reply.plist
launchctl load -w ~/Library/LaunchAgents/com.hh.update.plist
```
Проверить, что загружены:
```zsh
launchctl list | grep com.hh
```

## Управление
```zsh
# запустить прямо сейчас (не дожидаясь часа) — для проверки:
launchctl start com.hh.apply
launchctl start com.hh.reply
launchctl start com.hh.update         # учтёт стейт; для подъёма вне расписания: ./run-update-slot.sh --now

# логи:
tail -f logs/apply.log
tail -f logs/reply.log
tail -f logs/update.log

# снять с автозапуска:
launchctl unload -w ~/Library/LaunchAgents/com.hh.apply.plist
launchctl unload -w ~/Library/LaunchAgents/com.hh.reply.plist
launchctl unload -w ~/Library/LaunchAgents/com.hh.update.plist
```

## Дневной лимит откликов
- `apply` при лимите **после отправок** ставит паузу до завтра (+24ч 5м):
  файл `.state/apply_pause_until` (epoch). Пока пауза активна — часовые запуски
  apply пропускаются, а reply продолжает работать.
- если при возобновлении лимит ещё активен (0 отправлено) — пауза **не** ставится,
  apply пробует каждый час, пока лимит не сбросится.
- сбросить паузу вручную: `rm .state/apply_pause_until`.

## Подъём резюме (update)
- `com.hh.update` опрашивается launchd **каждые 15 мин**, но реальный подъём идёт
  **раз в 4:00–4:30**: следующий разрешённый момент лежит в `.state/update_next` (epoch),
  до него тики молча пропускаются (в лог не шумят). Джиттер 0–30 мин рандомится при
  каждом успешном подъёме — чтобы не дёргать hh ровно по таймеру.
- hh сам отдаёт `can_publish_or_update`: если 4ч с прошлого подъёма не прошли, резюме
  пропускается с предупреждением в лог.
- поднять немедленно (игнорируя стейт): `./run-update-slot.sh --now`.
- сбросить расписание вручную: `rm .state/update_next`.

## Важно
- **PATH:** в плистах задан `PATH` с `/usr/local/bin` и `/opt/homebrew/bin`
  (poetry/docker/ollama/curl). Если твои бинари в другом месте — поправь `PATH`
  в плистах.
- **НЕ держи репозиторий в `~/Desktop`/`~/Documents`/`~/Downloads`** — это
  TCC-защищённые папки, и launchd-агент не сможет читать/запускать скрипты оттуда
  (`Operation not permitted`). Поэтому проект живёт в `~/hh-applicant-tool`.
- **Путь к репозиторию** зашит в плистах абсолютным (`/Users/yanik/hh-applicant-tool`).
  Переедет репозиторий — обнови пути во всех плистах.
- **Сон/выключение мака:** пропущенный часовой запуск launchd выполнит один раз
  при пробуждении; на выключенном маке — пропуск.
- **Ollama** должен быть поднят (для reply) — иначе reply упадёт с понятной ошибкой.
- После правки плистов: `unload -w` → `load -w` заново.
