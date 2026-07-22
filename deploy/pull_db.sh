#!/bin/sh
# Стянуть СВЕЖУЮ консистентную копию канонической серверной parkrun.db на локаль.
#
# Открывать потом в DBeaver: data/parkrun_server.db (SQLite).
# Запускать сколько угодно — каждый раз перезаписывает свежим снимком.
#
# Почему не просто scp файла: база в WAL-режиме и в неё пишут воркеры, поэтому
# берём консистентный снимок через sqlite3 .backup на сервере, потом качаем.
# Один SSH-коннект (снимок + gzip-стрим), чтобы не ловить блокировку авторизации.
set -e

: "${PM_SSH_PASSWORD:?PM_SSH_PASSWORD не задан (лежит в .env репозитория)}"
HOST="viewer@195.58.34.112"
export SSHPASS="$PM_SSH_PASSWORD"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=20 \
  -o ControlMaster=auto -o ControlPath=/tmp/pm_ssh_ctl_%r@%h:%p -o ControlPersist=60m"

DEST="$(cd "$(dirname "$0")/.." && pwd)/data/parkrun_server.db"
mkdir -p "$(dirname "$DEST")"

echo "Снимаю консистентную копию на сервере и качаю..."
# shellcheck disable=SC2086
sshpass -e ssh $SSH_OPTS "$HOST" '
  ~/parkrun-monitoring/.venv/bin/python -c "
import sqlite3, pathlib
src = sqlite3.connect(pathlib.Path.home()/\"parkrun-monitoring/data/parkrun.db\")
dst = sqlite3.connect(\"/tmp/pm_snapshot.db\")
src.backup(dst); dst.close(); src.close()
"
  gzip -c /tmp/pm_snapshot.db
  rm -f /tmp/pm_snapshot.db
' | gunzip -c > "$DEST"

SIZE=$(du -h "$DEST" | cut -f1)
echo "Готово: $DEST ($SIZE)"
echo "Открой этот файл в DBeaver как SQLite."
