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
MAX_CONSEC_ERR = 5         # столько ошибок связи подряд = сдохший прокси
ERR_COOLDOWN_SEC = 600     # увод мёртвого выхода на 10 мин (без эскалации бана)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _limit_for(acc: str) -> int:
    """Лимит одновременных потоков для аккаунта. Пер-аккаунтный override через
    PM_LIMIT_<ACCOUNT> (напр. PM_LIMIT_FREE=15), иначе общий PM_ACCOUNT_LIMIT."""
    return int(os.getenv(f"PM_LIMIT_{acc.upper()}", os.getenv("PM_ACCOUNT_LIMIT", "3")))


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
    """Один VPN-выход: claim→fetch→store в цикле. При бане (3 капчи) ставит
    cooldown и ЗАВЕРШАЕТСЯ — супервайзер освободит слот аккаунта под замену."""
    conn = psycopg.connect(os.environ["PM_WORLD_DSN"], autocommit=False)
    consec_waf = 0
    consec_err = 0
    try:
        proxy, delay = conn.execute(
            "SELECT proxy, delay_sec FROM sweep_exits WHERE name=%s", (name,)).fetchone()
        conn.commit()
        client = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
                              proxy=proxy, timeout=30.0, follow_redirects=True)
        while not stop.is_set():
            delay = conn.execute("SELECT delay_sec FROM sweep_exits WHERE name=%s", (name,)).fetchone()[0]
            conn.commit()
            aid = claim(conn, name, 60)
            if aid is None:
                stop.wait(120); continue
            try:
                kind = process_athlete(conn, client, aid)
            except Exception as exc:
                conn.execute("UPDATE crawl_queue SET status='pending', claimed_by=NULL, "
                             "attempts=attempts+1, error=%s WHERE athlete_id=%s", (repr(exc)[:200], aid))
                conn.commit()
                kind = "error"
            if kind == "error":
                # Ошибка связи (частый случай для мёртвого free-прокси). Серия
                # подряд → короткий cooldown без эскалации бана, слот освобождён.
                consec_err += 1
                if consec_err >= MAX_CONSEC_ERR:
                    conn.execute("UPDATE sweep_exits SET cooldown_until=now() + "
                                 "(%s || ' seconds')::interval WHERE name=%s",
                                 (ERR_COOLDOWN_SEC, name))
                    conn.commit()
                    print(f"[{name}] {MAX_CONSEC_ERR} ошибок связи подряд → cooldown {ERR_COOLDOWN_SEC//60}м", flush=True)
                    return
                stop.wait(delay * random.uniform(0.85, 1.15))
                continue
            consec_err = 0
            if kind == "protected":
                consec_waf += 1
                conn.execute("UPDATE crawl_queue SET status='pending', claimed_by=NULL WHERE athlete_id=%s", (aid,))
                conn.commit()
                if consec_waf >= MAX_CONSEC_WAF:
                    record_ban(conn, name, delay)
                    print(f"[{name}] {MAX_CONSEC_WAF} капчи на {delay:.0f}с → cooldown, слот освобождён", flush=True)
                    return
            else:
                conn.execute("UPDATE sweep_exits SET last_ok_at=now(), "
                             "collected_total=collected_total+1, "
                             "active_seconds=active_seconds+%s, "
                             "ban_level=CASE WHEN ban_level>0 THEN 0 ELSE ban_level END WHERE name=%s",
                             (int(delay), name))
                conn.commit()
                maybe_tune(conn, name)
                consec_waf = 0
            stop.wait(delay * random.uniform(0.85, 1.15))
    finally:
        conn.close()


def main() -> None:
    stop = threading.Event()
    sup = psycopg.connect(os.environ["PM_WORLD_DSN"], autocommit=True)
    running: dict[str, threading.Thread] = {}
    try:
        while not stop.is_set():
            for n in [n for n, t in running.items() if not t.is_alive()]:
                del running[n]
            # аккаунты перечитываем каждый цикл. 'free' исключён — бесплатными
            # прокси рулит отдельный async-процесс (free_collector.py).
            accounts = [r[0] for r in sup.execute(
                "SELECT DISTINCT account FROM sweep_exits WHERE enabled AND account <> 'free'")]
            for acc in accounts:
                limit = _limit_for(acc)
                active = [n for n in running if _ACC.get(n) == acc]
                need = limit - len(active)
                if need <= 0:
                    continue
                avail = [r[0] for r in sup.execute(
                    """SELECT name FROM sweep_exits WHERE enabled AND account=%s
                       AND (cooldown_until IS NULL OR cooldown_until<=now())
                       ORDER BY delay_sec ASC, active_seconds ASC, name""", (acc,))]
                for name in [a for a in avail if a not in running][:need]:
                    _ACC[name] = acc
                    t = threading.Thread(target=exit_thread, args=(name, stop), daemon=True, name=name)
                    t.start(); running[name] = t
                    print(f"[{acc}] +поток {name} (активно {len([x for x in running if _ACC.get(x)==acc])}/{limit})", flush=True)
            stop.wait(30)
    except KeyboardInterrupt:
        stop.set()


_ACC: dict[str, str] = {}


if __name__ == "__main__":
    main()
