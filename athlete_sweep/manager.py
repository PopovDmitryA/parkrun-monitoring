#!/usr/bin/env python3
"""Менеджер обхода атлетов: один долгоживущий процесс, поток на каждый VPN-выход
из реестра sweep_exits. Каждый поток: claim → fetch(2 стр.) → store, со своей
задержкой; при 3 капчах подряд — cooldown по эскалирующей лестнице, память об
уровне бана (пол задержки = n+1), суточное снижение задержки, если держит.

Запуск (сервер, PM_WORLD_DSN в env): python -m athlete_sweep.manager
Жив под watchdog-cron (раз в 5 мин: не запущен → поднять).
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
from datetime import datetime, timezone

import httpx
import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from athlete_sweep.parse import AthleteData, parse_all_runs, parse_summary  # noqa: E402
from athlete_sweep.worker import UA, claim, fetch, store  # noqa: E402

# Эскалирующая лестница охлаждения: 1ч,3ч,6ч,12ч,24ч,3д,7д,14д (сек).
LADDER = [3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600, 3 * 86400, 7 * 86400, 14 * 86400]
DELAY_CEIL = 25.0          # потолок задержки
DELAY_STEP_DOWN = 1.0      # суточное снижение, если держит
DELAY_STEP_UP = 2.0        # подъём при бане
MAX_CONSEC_WAF = 3         # столько капч подряд = бан выхода
TUNE_EVERY_SEC = 86400     # раз в сутки корректируем задержку


def _now() -> datetime:
    return datetime.now(timezone.utc)


def process_athlete(conn, client, aid: int) -> str:
    """Две страницы: summary (имя/классификация/волонтёрство) + /all (забеги).
    Пишет результат, возвращает kind. 'protected' — не записан (капча)."""
    base = f"https://www.parkrun.org.uk/parkrunner/{aid}/"
    kind, html = fetch(client, base)
    if kind == "protected":
        return "protected"
    data = AthleteData(status="not_found") if kind == "not_found" else parse_summary(html, str(aid))
    if data.status == "ok":
        time.sleep(1.0 + random.random())  # маленькая пауза между страницами
        kind2, html2 = fetch(client, base + "all/")
        if kind2 == "protected":
            return "protected"
        data.runs = parse_all_runs(html2, str(aid))
    raw = html if data.status == "unclassified" else None
    store(conn, aid, data, raw)
    conn.execute("UPDATE crawl_queue SET status=%s, claimed_by=NULL, fetched_at=now() "
                 "WHERE athlete_id=%s", (data.status, aid))
    conn.commit()
    return data.status


def maybe_tune(conn, name: str) -> None:
    """Раз в сутки: держал без капчи → delay−1 (но не ниже delay_floor)."""
    conn.execute(
        """UPDATE sweep_exits SET delay_sec = GREATEST(delay_floor, delay_sec - %s),
           last_tuned_at = now()
           WHERE name=%s AND (last_tuned_at IS NULL OR last_tuned_at < now() - interval '1 day')
             AND (last_waf_at IS NULL OR last_waf_at < now() - interval '1 day')
             AND delay_sec > delay_floor""",
        (DELAY_STEP_DOWN, name),
    )
    conn.commit()


def record_ban(conn, name: str, delay_at_ban: float) -> None:
    """Бан выхода: cooldown по лестнице, ban_level++, пол задержки = n+1,
    поднять текущую задержку."""
    lvl = conn.execute("SELECT ban_level FROM sweep_exits WHERE name=%s", (name,)).fetchone()[0]
    cd = LADDER[min(lvl, len(LADDER) - 1)]
    conn.execute(
        """UPDATE sweep_exits SET
           cooldown_until = now() + (%s || ' seconds')::interval,
           ban_level = ban_level + 1,
           delay_floor = GREATEST(delay_floor, %s),
           delay_sec = LEAST(%s, %s),
           last_waf_at = now()
           WHERE name=%s""",
        (cd, delay_at_ban + 1, DELAY_CEIL, delay_at_ban + DELAY_STEP_UP, name),
    )
    conn.commit()


def exit_thread(name: str, stop: threading.Event) -> None:
    conn = psycopg.connect(os.environ["PM_WORLD_DSN"], autocommit=False)
    client = None
    cur_proxy = None
    consec_waf = 0
    while not stop.is_set():
        row = conn.execute(
            "SELECT proxy, delay_sec, cooldown_until, enabled FROM sweep_exits WHERE name=%s",
            (name,),
        ).fetchone()
        conn.commit()
        proxy, delay, cooldown_until, enabled = row
        if not enabled:
            stop.wait(300); continue
        if cooldown_until and cooldown_until > _now():
            wait = min((cooldown_until - _now()).total_seconds(), 300)
            stop.wait(max(wait, 5)); continue
        maybe_tune(conn, name)
        if client is None or proxy != cur_proxy:
            if client:
                client.close()
            client = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
                                  proxy=proxy, timeout=30.0, follow_redirects=True)
            cur_proxy = proxy

        aid = claim(conn, name, 60)
        if aid is None:
            print(f"[{name}] очередь пуста", flush=True)
            stop.wait(120); continue
        try:
            kind = process_athlete(conn, client, aid)
        except Exception as exc:
            conn.execute("UPDATE crawl_queue SET status='pending', claimed_by=NULL, "
                         "attempts=attempts+1, error=%s WHERE athlete_id=%s", (repr(exc)[:200], aid))
            conn.commit()
            kind = "error"

        if kind == "protected":
            consec_waf += 1
            conn.execute("UPDATE crawl_queue SET status='pending', claimed_by=NULL WHERE athlete_id=%s", (aid,))
            conn.commit()
            if consec_waf >= MAX_CONSEC_WAF:
                record_ban(conn, name, delay)
                print(f"[{name}] {MAX_CONSEC_WAF} капчи подряд на {delay:.0f}с → cooldown", flush=True)
                consec_waf = 0
        else:
            if consec_waf or kind == "ok":
                conn.execute("UPDATE sweep_exits SET last_ok_at=now(), ban_level=0 WHERE name=%s "
                             "AND ban_level>0", (name,))
                conn.execute("UPDATE sweep_exits SET last_ok_at=now() WHERE name=%s", (name,))
                conn.commit()
            consec_waf = 0
        stop.wait(delay * random.uniform(0.85, 1.15))
    conn.close()


def main() -> None:
    names = [r[0] for r in psycopg.connect(os.environ["PM_WORLD_DSN"]).execute(
        "SELECT name FROM sweep_exits WHERE enabled ORDER BY name")]
    print(f"менеджер: {len(names)} выходов — {', '.join(names)}", flush=True)
    stop = threading.Event()
    threads = [threading.Thread(target=exit_thread, args=(n, stop), daemon=True, name=n) for n in names]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
