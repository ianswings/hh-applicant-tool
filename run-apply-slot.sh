#!/usr/bin/env bash
# Ежедневный запуск apply-slot с selective-прокси и нашим поисковым запросом.
#
# Гарантия: через прокси ходит ТОЛЬКО Anthropic (ANTHROPIC_PROXY_URL).
# hh.ru идёт напрямую. Для этого скрипт ЯВНО вычищает HTTP_PROXY/HTTPS_PROXY —
# иначе их подхватила бы requests-сессия hh (см. main.py::_get_proxies).
#
# Поисковый запрос и параметры поиска зашиты ниже (SEARCH_ARGS). Любой из них
# можно переопределить, дописав флаг — он добавляется ПОСЛЕ и перебивает дефолт:
#   ./run-apply-slot.sh                                  # боевой запуск
#   ./run-apply-slot.sh --review --dry-run --total-pages 1   # тест без отправки
set -euo pipefail

cd "$(dirname "$0")"

# 0. PID-lock (общий для apply/reply): мёртвый снимаем, зависший >3ч замещаем,
#    легитимный долгий прогон — пропускаем. См. lock.sh.
# shellcheck disable=SC1091
source "$(dirname "$0")/lock.sh"
acquire_lock || exit 0
echo "===== apply старт $(date '+%Y-%m-%d %H:%M:%S') ====="

# 0.1 Пауза по дневному лимиту: если стоит и ещё не истекла — пропускаем запуск.
PAUSE_FILE=".state/apply_pause_until"
mkdir -p .state
if [[ -f "$PAUSE_FILE" ]]; then
  until_ts="$(cat "$PAUSE_FILE" 2>/dev/null || echo 0)"
  if [[ "$until_ts" =~ ^[0-9]+$ ]] && (( $(date +%s) < until_ts )); then
    echo "⏸  apply на паузе до $(date -r "$until_ts" '+%Y-%m-%d %H:%M') (дневной лимит) — пропуск."
    exit 0
  fi
fi

# 1. Загружаем секреты из .env (ANTHROPIC_API_KEY, ANTHROPIC_PROXY_URL, BLANCVPN_OUTLINE_KEY)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  echo "⚠️  Нет .env — скопируй .env.example в .env и заполни ключи." >&2
  exit 1
fi

# 2. КРИТИЧНО: hh.ru должен ходить НАПРЯМУЮ. Глобальные HTTP(S)_PROXY убираем,
#    чтобы requests-сессия hh их не подхватила. Anthropic берёт прокси отдельно
#    из ANTHROPIC_PROXY_URL.
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy || true

# 3. Прокси (общий для Anthropic-капчи и Telegram). Режим — PROXY_MODE (.env).
PROXY_MODE="${PROXY_MODE:-${ANTHROPIC_PROXY_MODE:-external}}"
case "$PROXY_MODE" in
  blancvpn)
    echo "🔌 Режим blancvpn: поднимаю sslocal (docker compose)…"
    docker compose -f docker-compose-anthropic.yml up -d
    export PROXY_URL="socks5://127.0.0.1:1080"
    ;;
  external)
    : "${PROXY_URL:=${ANTHROPIC_PROXY_URL:-}}"   # совместимость со старым именем
    export PROXY_URL
    echo "🔌 Режим external: PROXY_URL=${PROXY_URL:-<пусто → напрямую>}"
    ;;
  *)
    echo "⚠️  Неизвестный PROXY_MODE='$PROXY_MODE' (ожидается external|blancvpn)" >&2
    exit 1
    ;;
esac

# 4. Наш поисковый запрос (язык запросов hh, одной строкой — без переносов).
#    Уходит в API hh как параметр text ДОСЛОВНО (см. apply_vacancies.py::_get_search_params).
SEARCH_QUERY='Name:( ( (python OR пайтон OR питон) AND (developer OR разработчик OR backend OR бэкенд OR инженер OR engineer OR программист) ) OR ( "ML engineer" OR "AI engineer" OR "Data engineer" OR "MLOps engineer" OR "ML-инженер" OR "AI-инженер" OR "ML разработчик" OR "AI разработчик" OR "ML-разработчик" OR "AI-разработчик" OR "machine learning engineer" OR "LLM engineer" OR "LLM разработчик" ) ) AND NOT Name:( "full stack" OR "full-stack" OR fullstack OR фуллстек OR фулстек OR "фулл-стек" OR "фул-стек" OR "полный стек" OR qa OR тестировщик OR тестировщица OR frontend OR фронтенд OR фронт OR "1С" OR 1c OR devops OR sre OR "data scientist" OR "data science" OR "data-scientist" OR junior OR джуниор OR джун OR стажёр OR стажер OR intern OR trainee ) AND DESCRIPTION:(python OR питон OR пайтон) AND NOT DESCRIPTION:(intern OR trainee OR стажёр OR стажер)'

# Параметры поиска (перенесены из старого config.yaml). Переопредели через "$@".
SEARCH_ARGS=(
  --search "$SEARCH_QUERY"
  --resume-id 060bb0abff103e7b970039ed1f783869335666   # от лица какого резюме откликаемся
  --area 113                  # Россия (1=Москва, 2=СПб; см. https://api.hh.ru/areas)
  --order-by publication_time
  --per-page 100
  --total-pages 5             # сколько страниц выдачи пройти за запуск
  --force-message             # письмо на КАЖДУЮ вакансию (нужно для compliance-слов и агентной строки)
  # --no-magic                # ВКЛЮЧИ после теста: отключает "умную" переинтерпретацию text у hh.
                              # Для точного query-language запроса часто нужнее с ним. Сравни выдачу.
)

# 4.1 Ollama нужна для основного пути (provider: local). Капча — через Anthropic.
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
if ! curl -s --max-time 5 "$OLLAMA_URL/api/tags" >/dev/null; then
  echo "⚠️  Ollama не отвечает на $OLLAMA_URL — подними: ollama serve (и ollama pull gemma4:e4b)." >&2
  exit 1
fi

# 5. Запуск. hh.ru — direct, Anthropic — через ANTHROPIC_PROXY_URL.
echo "🚀 apply-slot (+ доп. аргументы: $*)"
set +e
poetry run hh-applicant-tool apply-slot "${SEARCH_ARGS[@]}" "$@"
rc=$?
set -e

# 6. Обработка дневного лимита по exit-code:
#    10 — лимит ПОСЛЕ отправок → пауза до завтра (+24ч 5м, чтобы лимит точно сбросился);
#    11 — лимит при 0 отправок (ещё не сбросился) → НЕ паузим, повтор каждый час;
#    иначе — снимаем паузу.
if [[ $rc -eq 10 ]]; then
  until_ts=$(( $(date +%s) + 86400 + 300 ))
  echo "$until_ts" > "$PAUSE_FILE"
  echo "⛔ Дневной лимит (после отправок). Следующий apply: $(date -r "$until_ts" '+%Y-%m-%d %H:%M')."
elif [[ $rc -eq 11 ]]; then
  rm -f "$PAUSE_FILE"
  echo "⏳ Лимит ещё активен (0 отправлено) — паузу не ставим, повтор через час."
else
  rm -f "$PAUSE_FILE"
fi
echo "===== apply конец $(date '+%Y-%m-%d %H:%M:%S') (exit $rc) ====="
exit $rc
