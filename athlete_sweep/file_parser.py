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

Параллелизм: ``--threads N`` — N страниц одновременно, у каждого потока свой
SFTP-канал и свой коннект к БД поверх ОДНОЙ ssh-сессии (не плодим логины). Узкое
место — не процессор, а ожидание сети: на канале с задержкой (VPN) 8 потоков дают
~9x (замер: 13/мин → 119/мин). Выше 8 прирост почти пропадает — упирается в общий
ssh-транспорт; если нужно больше, запускай второй процесс парсера.

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
import threading
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
    dsn = f"postgresql://{PM_USER}:{PM_PASS}@127.0.0.1:{tun.local_bind_port}/{PM_DB}"
    return tun, dsn


def db_connect(dsn: str):
    """Свой коннект на поток: psycopg-соединение делить между потоками нельзя."""
    import psycopg

    return psycopg.connect(dsn, autocommit=False, connect_timeout=10)


def open_ssh(host: str, user: str, pwd: str):
    """ОДНА ssh-сессия на процесс. Потоки берут из неё по своему SFTP-каналу —
    так не плодим логины (серия параллельных авторизаций легко ловит блокировку
    на сервере), а каналы внутри транспорта работают независимо."""
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=user, password=pwd, timeout=15,
                allow_agent=False, look_for_keys=False)
    tr = cli.get_transport()
    if tr is not None:
        tr.set_keepalive(30)
    return cli


def sftp_channel(ssh):
    """Отдельный SFTP-канал (на поток) поверх общего ssh-транспорта."""
    return ssh.get_transport().open_sftp_client()


# ─────────────────────────── работа с очередью ───────────────────────────
def claim(conn, worker: str, n: int) -> list[int]:
    """Забронировать до n собранных строк: помечаем claimed_by/claimed_at, статус
    ОСТАВЛЯЕМ 'collected' (пока не распарсили — это честно «в обработке»). Берём
    свободные (claimed_at пуст/просрочен), под SKIP LOCKED — параллельные парсеры
    не столкнутся."""
    # Ключ на claimed_by, НЕ на claimed_at: коллектор при пометке 'collected'
    # чистит claimed_by=NULL, но claimed_at оставляет от фазы dispatch (свежий).
    # Фильтр по claimed_at пропускал бы свежесобранное на 15 мин → парсер «пусто».
    # Берём: свободные (claimed_by NULL) + брошенные другим парсером (аренда истекла).
    rows = conn.execute(
        """
        WITH c AS (
            SELECT athlete_id FROM crawl_queue
            WHERE status='collected'
              AND (claimed_by IS NULL
                   OR (claimed_by LIKE 'parser:%%'
                       AND claimed_at < now() - make_interval(mins => %s)))
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


class Shared:
    """Общее состояние потоков: счётчики, статистика, флаг остановки, замок печати."""

    def __init__(self, limit: int) -> None:
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.limit = limit
        self.done = 0
        self.taken = 0            # слотов ЗАНЯТО (не дожидаясь результата)
        self.counts: dict[str, int] = {}

    def take_slot(self) -> bool:
        """Атомарно занять место под ещё одного атлета. Считаем именно взятые
        слоты, а не завершённые: иначе 8 потоков успевают проскочить проверку
        одновременно и --limit 20 превращается в 27."""
        with self.lock:
            if self.limit and self.taken >= self.limit:
                return False
            self.taken += 1
            return True

    def release_slot(self) -> None:
        """Вернуть слот, если атлет так и не был обработан (пропуск/сбой)."""
        with self.lock:
            if self.taken > 0:
                self.taken -= 1

    def has_room(self) -> bool:
        """Проверка без занятия слота (перед тем как идти за новой бронью)."""
        with self.lock:
            return not self.limit or self.taken < self.limit

    def record(self, status: str) -> int:
        with self.lock:
            self.done += 1
            self.counts[status] = self.counts.get(status, 0) + 1
            return self.done

    def say(self, msg: str) -> None:
        with self.lock:
            print(msg, flush=True)


def worker_loop(idx: int, dsn: str, ssh, args, st: Shared) -> None:
    """Один поток: свой коннект к БД, свой SFTP-канал, свой claim."""
    name = f"parser:{socket.gethostname()}:{os.getpid()}:t{idx}"
    try:
        conn = db_connect(dsn)
        sftp = sftp_channel(ssh)
    except Exception as exc:  # noqa: BLE001
        st.say(f"  поток {idx}: не поднялся ({exc!r})")
        return
    try:
        while not st.stop.is_set():
            if not st.has_room():
                break
            try:
                ids = claim(conn, name, args.batch)
            except Exception as exc:  # noqa: BLE001
                st.say(f"  поток {idx}: сбой брони {exc!r}")
                break
            if not ids:
                if args.once:
                    break
                # ждём новых собранных, но остаёмся отзывчивыми к Ctrl+C
                if st.stop.wait(IDLE_SLEEP):
                    break
                continue
            for aid in ids:
                if st.stop.is_set() or not st.take_slot():
                    break
                t0 = time.time()
                try:
                    status, desc = process_one(conn, sftp, args.raw_dir, aid, args.delete)
                except Exception as exc:  # noqa: BLE001 — одна строка не роняет прогон
                    try:
                        conn.rollback()
                        mark_retry(conn, aid, repr(exc))
                    except Exception:
                        pass
                    st.release_slot()
                    st.say(f"  атлет {aid}: сбой {exc!r}")
                    continue
                if status == "skip":
                    st.release_slot()
                    st.say(f"  атлет {aid}: {desc}")
                    continue
                n = st.record(status)
                st.say(f"  #{n} атлет {aid}: {desc} [{time.time()-t0:.2f}с]")
    finally:
        for c in (conn, sftp):
            try:
                c.close()
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Офлайн-парсер собранных parkrun-страниц")
    ap.add_argument("--limit", type=int, default=0, help="сколько атлетов (0 = без предела)")
    ap.add_argument("--batch", type=int, default=BATCH, help="размер брони за раз")
    ap.add_argument("--threads", type=int, default=1,
                    help="сколько страниц обрабатывать параллельно (1 = как раньше)")
    ap.add_argument("--delete", action="store_true",
                    help="удалять файлы сырья после успешной записи (по умолчанию НЕТ)")
    ap.add_argument("--raw-dir", default=os.getenv("PM_RAW_DIR", DEFAULT_RAW_DIR),
                    help="папка сырья на сервере")
    ap.add_argument("--once", action="store_true", help="разобрать что есть и выйти (не ждать)")
    args = ap.parse_args()
    threads = max(1, args.threads)

    host = _cred("PM_SSH_HOST", "TEMP_SSH_HOST", "PROD_SSH_HOST",
                 default="195.58.34.112", prompt="SSH-хост сервера: ")
    user = _cred("PM_SSH_USER", "TEMP_SSH_USER", "PROD_SSH_USER",
                 default="viewer", prompt="SSH-пользователь: ")
    pwd = _cred("PM_SSH_PASS", "TEMP_SSH_PASSWORD",
                secret=True, prompt="SSH-пароль: ")
    if not pwd:
        sys.exit("нет SSH-пароля (PM_SSH_PASS / .env / ввод) — прервано")

    print(f"[{platform.system()}] парсер {socket.gethostname()}:{os.getpid()} · "
          f"потоков: {threads}", flush=True)
    print(f"подключаюсь к {user}@{host} (БД-туннель + SFTP)…", flush=True)
    tun, dsn = open_db(host, user, pwd)
    ssh = open_ssh(host, user, pwd)
    print(f"на связи · папка {args.raw_dir} · удаление файлов: "
          f"{'да' if args.delete else 'нет'}", flush=True)

    st = Shared(args.limit)
    t0 = time.time()
    pool = [threading.Thread(target=worker_loop, args=(i + 1, dsn, ssh, args, st),
                             name=f"parser-{i+1}", daemon=True)
            for i in range(threads)]
    for t in pool:
        t.start()
    try:
        # join с таймаутом: голый join не пускает KeyboardInterrupt в главный поток
        while any(t.is_alive() for t in pool):
            for t in pool:
                t.join(0.3)
    except KeyboardInterrupt:
        print("\nостанавливаю потоки…", flush=True)
        st.stop.set()
        for t in pool:
            t.join(10)
    finally:
        st.stop.set()
        el = time.time() - t0
        summary = ", ".join(f"{k}={v}" for k, v in sorted(st.counts.items())) or "—"
        rate = st.done / el * 60 if el > 0 else 0
        print(f"итого распарсено: {st.done} ({summary}) за {el:.0f}с "
              f"≈ {rate:.0f}/мин", flush=True)
        for closer in (ssh, tun):
            try:
                closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nОтмена.")
