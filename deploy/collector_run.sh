#!/usr/bin/env bash
# Collector: run one queue worker per VPN country, in parallel.
#
# Reads PM_WORKERS from the environment / .env — a comma-separated list of
# worker:proxy pairs, e.g.:
#   PM_WORKERS="de:http://127.0.0.1:10811,nl:http://127.0.0.1:10812"
# A pair without a proxy (just "name") runs without one.
#
# Designed for cron: a lock file prevents overlapping runs, each worker's
# output goes to data/worker_<name>.log, and workers coordinate through the
# claims table so parallel workers never fetch the same location.
set -u

cd "$(dirname "$0")/.." || exit 1

# Overlap guard: skip this cron tick if the previous run is still going.
LOCK=data/collector.lock
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "collector already running (pid $(cat "$LOCK")), skipping"
  exit 0
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# .env is the single source of configuration (same file the CLI reads).
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

WORKERS="${PM_WORKERS:-}"
LIMIT="${PM_WORKER_LIMIT:-40}"
if [ -z "$WORKERS" ]; then
  echo "PM_WORKERS is not set (e.g. de:http://127.0.0.1:10811,nl:...)" >&2
  exit 1
fi

pids=()
IFS=',' read -ra pairs <<< "$WORKERS"
for pair in "${pairs[@]}"; do
  name="${pair%%:*}"
  proxy="${pair#"$name"}"; proxy="${proxy#:}"
  args=(work --worker "$name" --limit "$LIMIT")
  [ -n "$proxy" ] && args+=(--proxy "$proxy")
  echo "starting worker $name ${proxy:+via $proxy}"
  .venv/bin/parkrun-monitoring "${args[@]}" >> "data/worker_${name}.log" 2>&1 &
  pids+=($!)
done

status=0
for pid in "${pids[@]}"; do
  wait "$pid" || status=1
done
echo "collector run finished (exit $status)"
exit "$status"
