#!/usr/bin/env bash
# Подъём резюме в поиске hh.ru (POST /resumes/{id}/publish, см. update_resumes.py).
#
# ОТЛИЧИЯ от apply/reply:
#   - НЕ берёт общий lock (/tmp/hh-tool.lock) → запускается параллельно с apply/reply.
#     Это один короткий POST на резюме, не длинный прогон — конкуренции за токен почти нет.
#   - НЕ нужны Anthropic / Ollama: hh.ru ходит напрямую, LLM не задействован.
#   - .env грузится ТОЛЬКО ради Telegram-отчёта (TELEGRAM_BOT_TOKEN/CHAT_ID + PROXY_URL:
#     Telegram из РФ режется, шлём через тот же прокси, что Anthropic). Нет .env / секретов —
#     подъём всё равно отработает, просто без отчёта в Telegram.
#   - Свой lock (/tmp/hh-update.lock) — только чтобы два update не наложились друг на друга.
#
# Рандомный интервал (вариант B, стейт-файл): launchd опрашивает скрипт каждые 15 мин,
# а реальный подъём идёт раз в 4:00–4:30. Следующий разрешённый момент хранится в
# .state/update_next (epoch). До него запуски молча пропускаются. Это переживает сон мака
# (сверка по стенным часам, а не по таймеру) и не держит висящих процессов.
#
# Использование:
#   ./run-update-slot.sh            # боевой подъём (по расписанию из стейт-файла)
#   ./run-update-slot.sh --now      # игнорировать стейт, поднять прямо сейчас (для проверки)
set -euo pipefail

cd "$(dirname "$0")"

# 0. Свой PID-lock (НЕ общий с apply/reply): защита только от наложения двух update.
LOCK="/tmp/hh-update.lock"
if mkdir "$LOCK" 2>/dev/null; then
  echo "$$" > "$LOCK/pid"
  trap 'rm -rf "$LOCK"' EXIT
else
  oldpid="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [[ -n "$oldpid" ]] && kill -0 "$oldpid" 2>/dev/null; then
    echo "⏳ update уже идёт (pid $oldpid) — выходим." >&2
    exit 0
  fi
  echo "🧹 Держатель lock мёртв — снимаю протухший lock." >&2
  rm -rf "$LOCK"
  mkdir "$LOCK" && echo "$$" > "$LOCK/pid"
  trap 'rm -rf "$LOCK"' EXIT
fi

# 1. Рандомный интервал через стейт-файл. --now пропускает проверку.
STATE_FILE=".state/update_next"
mkdir -p .state
FORCE_NOW=0
for a in "$@"; do [[ "$a" == "--now" ]] && FORCE_NOW=1; done

if [[ "$FORCE_NOW" -eq 0 && -f "$STATE_FILE" ]]; then
  next_ts="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  if [[ "$next_ts" =~ ^[0-9]+$ ]] && (( $(date +%s) < next_ts )); then
    # Тихий выход — таких пропусков большинство (опрос каждые 15 мин). В лог не шумим.
    exit 0
  fi
fi

echo "===== update старт $(date '+%Y-%m-%d %H:%M:%S') ====="

# 2. .env — опционально, ТОЛЬКО ради Telegram-отчёта (TELEGRAM_*, PROXY_URL).
#    Нет файла — не падаем: подъём важнее отчёта.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
# PROXY_URL для Telegram: поддержим старое имя ANTHROPIC_PROXY_URL (как в apply/reply).
: "${PROXY_URL:=${ANTHROPIC_PROXY_URL:-}}"
export PROXY_URL

# 3. Подъём. КРИТИЧНО: hh.ru ходит НАПРЯМУЮ — глобальные HTTP(S)_PROXY вычищаем,
#    чтобы requests-сессия hh их не подхватила (Telegram берёт PROXY_URL отдельно).
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy || true
set +e
poetry run hh-applicant-tool update
rc=$?
set -e

# 4. Планируем следующий подъём: 4ч + рандомный джиттер 0–30 мин (избегаем ровного шага).
#    Пишем только при успехе — при сбое следующий 15-мин тик повторит попытку.
if [[ "$rc" -eq 0 ]]; then
  jitter=$(( RANDOM % 1800 ))                 # 0..1799 сек ≈ 0..30 мин
  next_ts=$(( $(date +%s) + 14400 + jitter )) # 4ч + джиттер
  echo "$next_ts" > "$STATE_FILE"
  echo "🗓  Следующий подъём не раньше $(date -r "$next_ts" '+%Y-%m-%d %H:%M') (+$(( (14400 + jitter) / 60 )) мин)."
else
  echo "⚠️  update завершился с ошибкой (exit $rc) — стейт не сдвигаем, повтор через ~15 мин." >&2
fi
echo "===== update конец $(date '+%Y-%m-%d %H:%M:%S') (exit $rc) ====="
exit $rc
