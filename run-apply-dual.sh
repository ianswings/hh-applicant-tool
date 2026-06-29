#!/usr/bin/env bash
# Ежедневный запуск apply-slot ДВУМЯ резюме за один проход (две ниши):
#   1) Python Backend  — резюме «Python-разработчик»;
#   2) AI / LLM Engineer — резюме «AI / LLM Engineer (Python, LangGraph, RAG)».
#
# Откат к одиночному сценарию: верни launchd на run-apply-slot.sh (он не тронут).
#
# Почему один скрипт, а не два процесса: общий PID-lock (lock.sh), общий
# hh-токен/config.json и общий дневной лимит откликов на аккаунт — параллелить
# нельзя. Поэтому оба прогона идут ПОСЛЕДОВАТЕЛЬНО внутри одного lock.
#
# Разделение ниш (AI-резюме забирает, если ВЫПОЛНЕНО ХОТЯ БЫ ОДНО):
#   - AI/ML/LLM/Data-роль в НАЗВАНИИ (ML/AI/LLM/MLOps/Data engineer, инженер
#     данных, big data, DWH…) — даже без AI-сигнала в описании (так берём Data
#     Engineer / дата-инженерию резюме AI Engineer);
#   - ЛИБО AI-сигнал (AISIG: LLM/RAG/LangGraph/…) в ОПИСАНИИ — так ловим вакансии
#     с backend-названием «Python Backend Developer», но AI-требованиями.
# Backend-резюме забирает остальное: backend-название БЕЗ AI/Data-роли и БЕЗ
# AISIG в описании. Условия зеркальны → выдачи НЕ пересекаются (нет гонки за
# вакансию и двойных откликов). Проверено: пересечение 0, покрытие старого ≈100%.
set -euo pipefail

cd "$(dirname "$0")"

# 0. PID-lock (общий для apply/reply): мёртвый снимаем, зависший >3ч замещаем,
#    легитимный долгий прогон — пропускаем. См. lock.sh.
# shellcheck disable=SC1091
source "$(dirname "$0")/lock.sh"
acquire_lock || exit 0
echo "===== apply-dual старт $(date '+%Y-%m-%d %H:%M:%S') ====="

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

# 4. Компоненты поисковых запросов (язык запросов hh). Объявлены ОДИН раз,
#    переиспользуются в обоих запросах — общий мусор-фильтр и AISIG не дублируем.

# 4.1 Роли в названии (Name).
NAME_BACKEND="( (python OR пайтон OR питон) AND (developer OR разработчик OR backend OR бэкенд OR инженер OR engineer OR программист) )"
# AI/ML/LLM + Data-инженерия — всё это ниша резюме «AI / LLM Engineer». Data-роли
# (Data engineer, инженер данных, big data, DWH…) включены ПО НАЗВАНИЮ: такие
# вакансии обычно без AISIG в описании, поэтому ловим их по роли, а не по сигналу.
NAME_AI_DATA='( "ML engineer" OR "AI engineer" OR "MLOps engineer" OR "MLOps-инженер" OR "MLOps инженер" OR "ML-инженер" OR "AI-инженер" OR "ML разработчик" OR "AI разработчик" OR "ML-разработчик" OR "AI-разработчик" OR "machine learning engineer" OR "LLM engineer" OR "LLM разработчик" OR "Data engineer" OR "инженер данных" OR "инженер по данным" OR "дата-инженер" OR "дата инженер" OR "data engineering" OR "big data" OR DWH OR "data platform" )'

# 4.2 Мусор в названии — общий стоп-лист (NOT Name) для обоих прогонов.
MUSOR='"full stack" OR "full-stack" OR fullstack OR фуллстек OR фулстек OR "фулл-стек" OR "фул-стек" OR "полный стек" OR qa OR тестировщик OR тестировщица OR frontend OR фронтенд OR фронт OR "1С" OR 1c OR devops OR sre OR "data scientist" OR "data science" OR "data-scientist" OR junior OR джуниор OR джун OR стажёр OR стажер OR intern OR trainee'

# 4.3 AISIG — маркеры AI/LLM-вакансии В ОПИСАНИИ. Это ГРАНИЦА между нишами:
#     есть сигнал → AI-резюме, нет → backend-резюме. Расширишь список — больше
#     вакансий уйдёт в AI-нишу (и сузится backend). Сейчас: ядро LLM + ML.
AISIG='LLM OR "large language model" OR RAG OR LangChain OR LangGraph OR LlamaIndex OR агентн* OR agentic OR эмбеддинг* OR embeddings OR векторн* OR Qdrant OR pgvector OR vLLM OR промпт* OR prompt OR "fine-tuning" OR дообучени* OR OpenAI OR Anthropic OR Claude OR GPT OR "машинное обучение" OR "machine learning" OR ML'

# 4.4 Итоговые запросы (одной строкой, уходят в API hh как параметр text дословно).
#   Backend: backend-роль в названии, БЕЗ AI/Data-роли и БЕЗ AI-сигнала в описании.
SEARCH_BACKEND="Name:( $NAME_BACKEND ) AND NOT Name:( $MUSOR OR $NAME_AI_DATA ) AND DESCRIPTION:(python OR питон OR пайтон) AND NOT DESCRIPTION:( $AISIG ) AND NOT DESCRIPTION:(intern OR trainee OR стажёр OR стажер)"
#   AI: AI/ML/LLM/Data-роль в названии ЛИБО backend-название + AI-сигнал в описании.
SEARCH_AI="( Name:( $NAME_AI_DATA ) OR ( Name:( $NAME_BACKEND ) AND DESCRIPTION:( $AISIG ) ) ) AND NOT Name:( $MUSOR ) AND DESCRIPTION:(python OR питон OR пайтон) AND NOT DESCRIPTION:(intern OR trainee OR стажёр OR стажер)"

# Резюме hh, от лица которых откликаемся (см. list-resumes).
RESUME_BACKEND="060bb0abff103e7b970039ed1f783869335666"   # Python-разработчик
RESUME_AI="c321ae99ff10b4bb2f0039ed1f515535474b79"        # AI / LLM Engineer (Python, LangGraph, RAG)

# Общие параметры поиска для обоих прогонов. Переопредели через "$@".
COMMON_ARGS=(
  --area 113                  # Россия (1=Москва, 2=СПб; см. https://api.hh.ru/areas)
  --order-by publication_time
  --per-page 100
  --total-pages 5             # сколько страниц выдачи пройти за запуск
  --force-message             # письмо на КАЖДУЮ вакансию (нужно для compliance-слов и агентной строки)
  # --no-magic                # для СТРОГОГО разделения по полям/NOT включи после теста:
                              # без него hh «магически» переинтерпретирует text и может
                              # размыть границу ниш. Сравни выдачу с --review --dry-run.
)

# 4.5 Ollama нужна для основного пути (provider: local). Капча — через Anthropic.
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
if ! curl -s --max-time 5 "$OLLAMA_URL/api/tags" >/dev/null; then
  echo "⚠️  Ollama не отвечает на $OLLAMA_URL — подними: ollama serve (и ollama pull qwen2.5:14b-instruct)." >&2
  exit 1
fi

# 4.6 Watchdog: жёсткий потолок на прогон (bash-native, без coreutils/gtimeout).
#     Зачем: при зависшем сетевом вызове процесс висит вечно, а launchd НЕ
#     запускает новый агент, пока этот «жив» → расписание встаёт. Потолок убивает
#     зомби (TERM, через 30с KILL) и освобождает слот. set -m → каждая фоновая
#     команда в своей process-group, поэтому kill -- -PGID бьёт и poetry, и python.
WATCHDOG_SEC="${WATCHDOG_SEC:-2700}"   # 45 мин на ОДИН прогон apply-slot
run_with_watchdog() {                  # $1=лимит_сек, далее — команда
  local limit="$1"; shift
  set -m
  "$@" &
  local cmd=$!
  ( sleep "$limit"; kill -TERM -"$cmd" 2>/dev/null; sleep 30; kill -KILL -"$cmd" 2>/dev/null ) &
  local wd=$!
  wait "$cmd"; local rc=$?
  kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null   # снять будильник, если прогон закончился сам
  return "$rc"
}

# 5. Обработка дневного лимита по exit-code прогона:
#    10 — лимит ПОСЛЕ отправок → пауза до завтра (+24ч 5м), второй прогон не нужен;
#    11 — лимит при 0 отправок (ещё не сбросился) → НЕ паузим, повтор через час;
#    иначе — норма, паузу снимаем.
# Пауза при массовой капче (часы) — из config.yaml::apply.captcha_pause_hours, дефолт 2.
read_captcha_pause_hours() {
  local h
  h=$(poetry run python -c "import yaml;print(int(yaml.safe_load(open('src/hh_applicant_tool/our_extensions/config.yaml')).get('apply',{}).get('captcha_pause_hours',2)))" 2>/dev/null)
  [[ "$h" =~ ^[0-9]+$ ]] && echo "$h" || echo 2
}

handle_rc() {
  local rc="$1"
  if [[ $rc -eq 10 ]]; then
    local until_ts; until_ts=$(( $(date +%s) + 86400 + 300 ))
    echo "$until_ts" > "$PAUSE_FILE"
    echo "⛔ Дневной лимит (после отправок). Следующий apply: $(date -r "$until_ts" '+%Y-%m-%d %H:%M')."
  elif [[ $rc -eq 11 ]]; then
    rm -f "$PAUSE_FILE"
    echo "⏳ Лимит ещё активен (0 отправлено) — паузу не ставим, повтор через час."
  elif [[ $rc -eq 12 ]]; then
    local hours until_ts; hours="$(read_captcha_pause_hours)"
    until_ts=$(( $(date +%s) + hours * 3600 ))
    echo "$until_ts" > "$PAUSE_FILE"
    echo "⛔ Массовая капча (hh лимитирует аккаунт). Пауза ${hours}ч, следующий apply: $(date -r "$until_ts" '+%Y-%m-%d %H:%M')."
  else
    rm -f "$PAUSE_FILE"
  fi
}

# 6. Прогон 1 — Python Backend. hh.ru — direct, Anthropic — через ANTHROPIC_PROXY_URL.
echo "🚀 [1/2] apply-slot Backend (resume $RESUME_BACKEND) (+ доп. аргументы: $*)"
set +e
run_with_watchdog "$WATCHDOG_SEC" poetry run hh-applicant-tool apply-slot \
  --resume-id "$RESUME_BACKEND" --search "$SEARCH_BACKEND" "${COMMON_ARGS[@]}" "$@"
rc_backend=$?
set -e

# Лимит/капча общие на аккаунт: если backend упёрся — второй прогон бессмыслен.
if [[ $rc_backend -eq 10 || $rc_backend -eq 11 || $rc_backend -eq 12 ]]; then
  echo "⚠️  Backend-прогон остановлен (rc=$rc_backend: лимит/капча) — AI-прогон пропускаем."
  handle_rc "$rc_backend"
  echo "===== apply-dual конец $(date '+%Y-%m-%d %H:%M:%S') (backend=$rc_backend, ai=skipped) ====="
  exit "$rc_backend"
fi

# 7. Прогон 2 — AI / LLM Engineer.
echo "🚀 [2/2] apply-slot AI/LLM (resume $RESUME_AI) (+ доп. аргументы: $*)"
set +e
run_with_watchdog "$WATCHDOG_SEC" poetry run hh-applicant-tool apply-slot \
  --resume-id "$RESUME_AI" --search "$SEARCH_AI" "${COMMON_ARGS[@]}" "$@"
rc_ai=$?
set -e

handle_rc "$rc_ai"
echo "===== apply-dual конец $(date '+%Y-%m-%d %H:%M:%S') (backend=$rc_backend, ai=$rc_ai) ====="
exit "$rc_ai"
