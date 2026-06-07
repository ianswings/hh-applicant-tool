#!/usr/bin/env bash
# Ведение переписки с работодателями: классификация (Ollama) + ответ (Anthropic).
#
# Selective-прокси как в run-apply-slot.sh: через прокси ходит ТОЛЬКО Anthropic,
# hh.ru — напрямую. Классификация — локально через Ollama (прокси не нужен).
#
# Использование:
#   ./run-reply-slot.sh --review --dry-run     # превью: класс + ответ, без отправки
#   ./run-reply-slot.sh                          # боевой (вопросы отвечаются авто)
set -euo pipefail

cd "$(dirname "$0")"

# 0. PID-lock (общий для apply/reply): мёртвый снимаем, зависший >3ч замещаем,
#    легитимный долгий прогон — пропускаем. См. lock.sh.
# shellcheck disable=SC1091
source "$(dirname "$0")/lock.sh"
acquire_lock || exit 0
echo "===== reply старт $(date '+%Y-%m-%d %H:%M:%S') ====="

# 1. .env
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  echo "⚠️  Нет .env — скопируй .env.example в .env и заполни ключи." >&2
  exit 1
fi

# 2. hh.ru — напрямую: чистим глобальные прокси (Anthropic берёт ANTHROPIC_PROXY_URL).
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy || true

# 3. Прокси (общий для Telegram-уведомлений; reply-ответы — локальные). Режим — PROXY_MODE.
PROXY_MODE="${PROXY_MODE:-${ANTHROPIC_PROXY_MODE:-external}}"
case "$PROXY_MODE" in
  blancvpn)
    echo "🔌 Режим blancvpn: поднимаю sslocal…"
    docker compose -f docker-compose-anthropic.yml up -d
    export PROXY_URL="socks5://127.0.0.1:1080"
    ;;
  external)
    : "${PROXY_URL:=${ANTHROPIC_PROXY_URL:-}}"
    export PROXY_URL
    echo "🔌 Режим external: PROXY_URL=${PROXY_URL:-<пусто → напрямую>}"
    ;;
  *)
    echo "⚠️  Неизвестный PROXY_MODE='$PROXY_MODE' (external|blancvpn)" >&2
    exit 1
    ;;
esac

# 4. Проверяем, что Ollama поднят (классификатор обязателен — иначе падаем).
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
if ! curl -s --max-time 5 "$OLLAMA_URL/api/tags" >/dev/null; then
  echo "⚠️  Ollama не отвечает на $OLLAMA_URL — подними: ollama serve (и ollama pull gemma4:26b)." >&2
  exit 1
fi

# 5. Запуск. hh — direct, Anthropic — через прокси, классификация — локально.
echo "🚀 reply-slot $*"
set +e
poetry run hh-applicant-tool reply-slot "$@"
rc=$?
set -e
echo "===== reply конец $(date '+%Y-%m-%d %H:%M:%S') (exit $rc) ====="
exit $rc
