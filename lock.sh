# Общий PID-lock для apply/reply: взаимное исключение + авто-восстановление.
#   - держатель мёртв (нет процесса) → снимаем и захватываем;
#   - жив и моложе MAX_RUNTIME → пропускаем (легитимный долгий прогон);
#   - жив, но висит дольше MAX_RUNTIME → убиваем и замещаем.
# Подключается через `source`, вызывается acquire_lock.

LOCK="/tmp/hh-tool.lock"
MAX_RUNTIME="${HH_LOCK_MAX_RUNTIME:-10800}"   # 3ч — дольше живого считаем зависшим

acquire_lock() {
  local oldpid age tries=0
  while (( tries++ < 5 )); do
    if mkdir "$LOCK" 2>/dev/null; then
      echo "$$" > "$LOCK/pid"
      trap 'rm -rf "$LOCK"' EXIT
      return 0
    fi
    oldpid="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [[ -n "$oldpid" ]] && kill -0 "$oldpid" 2>/dev/null; then
      # mtime lock'а; если каталог исчез между проверками (гонка) — stat пуст,
      # считаем lock протухшим и перезахватываем (иначе арифметика падала с
      # "operand expected").
      mtime="$(stat -f %m "$LOCK" 2>/dev/null)"
      if [[ ! "$mtime" =~ ^[0-9]+$ ]]; then
        rm -rf "$LOCK" 2>/dev/null
        continue
      fi
      age=$(( $(date +%s) - mtime ))
      if (( age > MAX_RUNTIME )); then
        echo "🔪 Прежний прогон висит $((age/60)) мин (pid $oldpid) — убиваю и замещаю." >&2
        kill -TERM "-$oldpid" 2>/dev/null || kill -TERM "$oldpid" 2>/dev/null || true
        sleep 3
        kill -KILL "-$oldpid" 2>/dev/null || kill -KILL "$oldpid" 2>/dev/null || true
        rm -rf "$LOCK"
        continue
      fi
      echo "⏳ apply/reply уже идёт (pid $oldpid, $((age/60)) мин) — выходим." >&2
      return 1
    fi
    echo "🧹 Держатель lock мёртв — снимаю протухший lock." >&2
    rm -rf "$LOCK"
  done
  echo "⏳ Не удалось захватить lock — выходим." >&2
  return 1
}
