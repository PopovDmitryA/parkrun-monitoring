#!/usr/bin/env bash
# Параллельный обход атлетов: по одному воркеру на VPN-выход.
#
# PM_SWEEP_WORKERS="de:http://127.0.0.1:10811,it:http://127.0.0.1:10814,..."
# PM_SWEEP_LIMIT — атлетов на воркера за прогон (по умолчанию 300).
# PM_SWEEP_DELAY — пауза между запросами, сек (по умолчанию 6, джиттер ±15%).
# PM_DISK_MIN_GB — стоп, если на / свободно меньше (по умолчанию 3).
#
# Для cron: замок против нахлёста, лог на воркера в data/sweep_<name>.log.
set -u
cd "$(dirname "$0")/.." || exit 1

if [ -f .env ]; then set -a; . ./.env; set +a; fi

# --- предохранитель по диску ---
MIN_GB="${PM_DISK_MIN_GB:-3}"
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if [ "${FREE_GB:-0}" -lt "$MIN_GB" ]; then
  echo "$(date '+%F %T') СТОП: свободно ${FREE_GB}ГБ < ${MIN_GB}ГБ — сбор приостановлен, нужен бамп диска" >&2
  exit 2
fi

LOCK=data/sweep.lock
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "sweep уже идёт (pid $(cat "$LOCK")), пропускаю"; exit 0
fi
echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT

WORKERS="${PM_SWEEP_WORKERS:-}"
LIMIT="${PM_SWEEP_LIMIT:-300}"
DELAY="${PM_SWEEP_DELAY:-6}"
[ -z "$WORKERS" ] && { echo "PM_SWEEP_WORKERS не задан" >&2; exit 1; }
mkdir -p data

pids=()
IFS=',' read -ra pairs <<< "$WORKERS"
for pair in "${pairs[@]}"; do
  name="${pair%%:*}"; proxy="${pair#"$name":}"
  echo "$(date '+%T') старт воркера $name через $proxy"
  .venv/bin/python athlete_sweep/worker.py --worker "$name" --proxy "$proxy" \
    --limit "$LIMIT" --delay "$DELAY" >> "data/sweep_${name}.log" 2>&1 &
  pids+=($!)
done
st=0
for pid in "${pids[@]}"; do wait "$pid" || st=1; done
echo "$(date '+%T') прогон завершён (exit $st, свободно ${FREE_GB}ГБ)"
exit "$st"
