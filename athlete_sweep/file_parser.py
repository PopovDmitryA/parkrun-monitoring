#!/usr/bin/env python3
"""Офлайн-парсер собранных parkrun-страниц. Кроссплатформенный (macOS + Windows).

Free-сборщик (free_collector.py, режим FETCH-ONLY) складывает сырьё на сервере в
папку data/raw: gzip-HTML вида ``{aid//10000}/{aid}.summary.html.gz`` (+ ``.all.``
у реальных атлетов) и помечает строку в crawl_queue статусом ``collected``. Этот
скрипт разбирает такие файлы С ЛЮБОГО компа: поднимает SSH-туннель к pm-postgres и
SFTP к папке через paramiko/sshtunnel (БЕЗ sshpass — чтобы шло и на Windows),
парсит, пишет в БД теми же ``parse``/``store``, что и краулер, помечает очередь
финальным статусом. Несколько парсеров (Мак + винда) работают параллельно —
строки разбираются через ``FOR UPDATE SKIP LOCKED`` + аренда claim.

Что НЕ трогаем: ``fetched_at`` (это время СБОРА, по нему считается «сбор/час» на
табло) — парсер его не переписывает, иначе метрика сбора врала бы. Прогресс
обработки виден на табло как «обработка/час» (по ``athletes.parsed_at``, его
ставит ``store``).

Запуск:
  macOS:    make parkrun → пункт 3   (или: python -m athlete_sweep.file_parser)
  Windows:  py -m athlete_sweep.file_parser   (из клона parkrun-monitoring)

Зависимости (оба ОС): pip install paramiko sshtunnel "psycopg[binary]" beautifulsoup4
Креды сервера — из переменных окружения (PM_SSH_HOST / PM_SSH_USER / PM_SSH_PASS)
или из соседнего .env (TEMP_SSH_* / PROD_SSH_*), иначе скрипт спросит интерактивно.
"""
from __future__ import annotations

import argparse
import gzip
import os
import platform
import socket
import sys
import time
from getpass import getpass

# parse.py/worker.store лежат рядом — гарантируем, что пакет athlete_sweep импортируем
# и при запуске файла напрямую (python athlete_sweep/file_parser.py), и как модуль.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from athlete_sweep.parse import AthleteData, parse_all_runs, parse_summary  # noqa: E402
from athlete_sweep.worker import store  # noqa: E402

PM_DB = "parkrun_world"
PM_USER = "parkrun"
PM_PASS = "parkrun_world_local"
REMOTE_DB_HOST = "127.0.0.1"
REMOTE_DB_PORT = 5433
DEFAULT_RAW_DIR = "/home/viewer/parkrun-monitoring/data/raw"
LEASE_MIN = 15          # чужой claim старше этого времени считаем брошенным
BATCH = 50              # сколько строк бронируем за раз
IDLE_SLEEP = 60         # пусто → пауза перед повтором, сек


# ─────────────────────────── креды сервера ───────────────────────────
def _env_files() -> list[str]:
    """Кандидаты .env: рядом с репо и в соседнем saturday_runs_stats (если есть)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = os.path.expanduser("~")
    return [
        os.path.join(root, ".env"),
        os.path.join(home, "Projects", "saturday_runs_stats", ".env"),
    ]


def _from_env_file(key: str) -> str:
    for path in _env_files():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _cred(env_key: str, *file_keys: str, default: str = "", secret: bool = False,
          prompt: str | None = None) -> str:
    val = os.getenv(env_key, "")
    if not val:
        for fk in file_keys:
            val = _from_env_file(fk)
            if val:
                break
    if not val and prompt:
        val = (getpass(prompt) if secret else input(prompt)).strip()
    return val or default


# ─────────────────────────── подключения ───────────────────────────
def open_db(host: str, user: str, pwd: str):
    """SSH-туннель к pm-postgres → локальный порт → psycopg-соединение."""
    from sshtunnel import SSHTunnelForwarder

    tun = SSHTunnelForwarder(
        (host, 22),
        ssh_username=user,
        ssh_password=pwd,
        remote_bind_address=(REMOTE_DB_HOST, REMOTE_DB_PORT),
        set_keepalive=30.0,
    )
    tun.start()
    import psycopg

    dsn = f"postgresql://{PM_USER}:{PM_PASS}@127.0.0.1:{tun.local_bind_port}/{PM_DB}"
    conn = psycopg.connect(dsn, autocommit=False, connect_timeout=10)
    return tun, conn


def open_sftp(host: str, user: str, pwd: str):
    """Отдельная paramiko-сессия под чтение/удаление файлов сырья."""
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=user, password=pwd, timeout=15,
                allow_agent=False, look_for_keys=False)
    tr = cli.get_transport()
    if tr is not None:
        tr.set_keepalive(30)
    return cli, cli.open_sftp()


# ─────────────────────────── работа с очередью ───────────────────────────
def claim(conn, worker: str, n: int) -> list[int]:
    """Забронировать до n собранных строк: помечаем claimed_by/claimed_at, статус
    ОСТАВЛЯЕМ 'collected' (пока не распарсили — это честно «в обработке»). Берём
    свободные (claimed_at пуст/просрочен), под SKIP LOCKED — параллельные парсеры
    не столкнутся."""
    rows = conn.execute(
        """
        WITH c AS (
            SELECT athlete_id FROM crawl_queue
            WHERE status='collected'
              AND (claimed_at IS NULL OR claimed_at < now() - make_interval(mins => %s))
            ORDER BY fetched_at
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        )
        UPDATE crawl_queue q SET claimed_by=%s, claimed_at=now()
        FROM c WHERE q.athlete_id=c.athlete_id
        RETURNING q.athlete_id
        """,
        (LEASE_MIN, n, worker),
    ).fetchall()
    conn.commit()
    return [int(r[0]) for r in rows]


def mark_done(conn, aid: int, status: str) -> None:
    # НЕ трогаем fetched_at (метрика сбора). Освобождаем claim, ставим финальный статус.
    conn.execute(
        "UPDATE crawl_queue SET status=%s, claimed_by=NULL, claimed_at=NULL WHERE athlete_id=%s",
        (status, aid),
    )


def mark_retry(conn, aid: int, err: str) -> None:
    conn.execute(
        "UPDATE crawl_queue SET claimed_by=NULL, claimed_at=NULL, attempts=attempts+1, "
        "error=%s WHERE athlete_id=%s",
        (err[:200], aid),
    )
    conn.commit()


# ─────────────────────────── файлы сырья ───────────────────────────
def remote_path(raw_dir: str, aid: int, kind: str) -> str:
    return f"{raw_dir}/{aid // 10000}/{aid}.{kind}.html.gz"


def read_html(sftp, path: str) -> str | None:
    try:
        with sftp.open(path, "rb") as fh:
            fh.prefetch()
            raw = fh.read()
    except IOError:
        return None
    return gzip.decompress(raw).decode("utf-8", "replace")


def delete_files(sftp, raw_dir: str, aid: int) -> None:
    for kind in ("summary", "all"):
        try:
            sftp.remove(remote_path(raw_dir, aid, kind))
        except IOError:
            pass


# ─────────────────────────── основной цикл ───────────────────────────
def process_one(conn, sftp, raw_dir: str, aid: int, delete: bool) -> tuple[str, str]:
    """Разобрать одного атлета из файлов. Возвращает (status, короткое-описание)."""
    summary = read_html(sftp, remote_path(raw_dir, aid, "summary"))
    if summary is None:
        # файла нет — вернём в 'collected' на дораскачку/пересбор
        mark_retry(conn, aid, "no summary file")
        return "skip", "нет summary-файла"

    data = parse_summary(summary, str(aid))
    if data.status == "ok":
        all_html = read_html(sftp, remote_path(raw_dir, aid, "all"))
        if all_html is not None:
            data.runs = parse_all_runs(all_html, str(aid))
    raw = summary if data.status == "unclassified" else None

    store(conn, aid, data, raw)
    mark_done(conn, aid, data.status)
    conn.commit()  # атлет + очередь одной транзакцией на строку

    if delete:
        delete_files(sftp, raw_dir, aid)
    return data.status, f"{data.name or data.status} ({data.status}, {data.total_runs or 0} заб.)"


def main() -> None:
    ap = argparse.ArgumentParser(description="Офлайн-парсер собранных parkrun-страниц")
    ap.add_argument("--limit", type=int, default=0, help="сколько атлетов (0 = без предела)")
    ap.add_argument("--batch", type=int, default=BATCH, help="размер брони за раз")
    ap.add_argument("--delete", action="store_true",
                    help="удалять файлы сырья после успешной записи (по умолчанию НЕТ)")
    ap.add_argument("--raw-dir", default=os.getenv("PM_RAW_DIR", DEFAULT_RAW_DIR),
                    help="папка сырья на сервере")
    ap.add_argument("--once", action="store_true", help="разобрать что есть и выйти (не ждать)")
    args = ap.parse_args()

    host = _cred("PM_SSH_HOST", "TEMP_SSH_HOST", "PROD_SSH_HOST",
                 default="195.58.34.112", prompt="SSH-хост сервера: ")
    user = _cred("PM_SSH_USER", "TEMP_SSH_USER", "PROD_SSH_USER",
                 default="viewer", prompt="SSH-пользователь: ")
    pwd = _cred("PM_SSH_PASS", "TEMP_SSH_PASSWORD",
                secret=True, prompt="SSH-пароль: ")
    if not pwd:
        sys.exit("нет SSH-пароля (PM_SSH_PASS / .env / ввод) — прервано")

    worker = f"parser:{socket.gethostname()}:{os.getpid()}"
    print(f"[{platform.system()}] парсер {worker}", flush=True)
    print(f"подключаюсь к {user}@{host} (БД-туннель + SFTP)…", flush=True)
    tun, conn = open_db(host, user, pwd)
    ssh, sftp = open_sftp(host, user, pwd)
    print(f"на связи · папка {args.raw_dir} · удаление файлов: "
          f"{'да' if args.delete else 'нет'}", flush=True)

    done = 0
    counts: dict[str, int] = {}
    try:
        while True:
            if args.limit and done >= args.limit:
                print(f"лимит {args.limit} достигнут.", flush=True)
                break
            take = args.batch
            if args.limit:
                take = min(take, args.limit - done)
            ids = claim(conn, worker, take)
            if not ids:
                if args.once:
                    print("собранных строк нет — выхожу (--once).", flush=True)
                    break
                print(f"собранных строк нет, жду {IDLE_SLEEP}с…", flush=True)
                time.sleep(IDLE_SLEEP)
                continue
            for aid in ids:
                t0 = time.time()
                try:
                    status, desc = process_one(conn, sftp, args.raw_dir, aid, args.delete)
                except Exception as exc:  # noqa: BLE001 — не роняем прогон из-за одной строки
                    conn.rollback()
                    mark_retry(conn, aid, repr(exc))
                    print(f"  атлет {aid}: сбой {exc!r}", flush=True)
                    continue
                if status == "skip":
                    print(f"  атлет {aid}: {desc}", flush=True)
                    continue
                done += 1
                counts[status] = counts.get(status, 0) + 1
                print(f"  #{done} атлет {aid}: {desc} [{time.time()-t0:.2f}с]", flush=True)
    except KeyboardInterrupt:
        print("\nостановлено.", flush=True)
    finally:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "—"
        print(f"итого распарсено: {done} ({summary})", flush=True)
        try:
            conn.close()
        except Exception:
            pass
        for closer in (sftp, ssh, tun):
            try:
                closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nОтмена.")
