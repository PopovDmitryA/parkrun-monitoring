#!/usr/bin/env python3
"""Async-сборщик атлетов parkrun через пул бесплатных публичных прокси.

Отдельный процесс (не VPN-менеджер): сотни прокси в ОДНОМ процессе на asyncio,
общий пул из ~15 коннектов к БД (не по коннекту на прокси!) — поэтому RAM
скромный и потолок max_connections не трогаем. Сам сайт на том же боксе не
страдает; число активных прокси растим рампой через PM_FREE_TARGET.

Устройство:
- пул `free_proxies` (липкий): валидированные адреса переживают рестарт;
- рабочий прокси, поймавший бан/ошибки, НЕ удаляется — уходит в СТУПЕНЧАТУЮ
  отлёжку (ban_level↑, лестница) и возвращается после неё (ротация);
- добор новых прокси: две ступени — сначала «жив ли» (generate_204), потом
  «отдаёт ли настоящий parkrun» (атлет 620 → штрихкод A620); в parkrun летят
  только уже-живые кандидаты (бережём его от лишнего долбления);
- воркер держит коннект к БД только на короткие claim/store, не на время фетча.

Запуск (сервер, PM_WORLD_DSN в env): python -m athlete_sweep.free_collector
Жив под watchdog-cron. Останов — SIGTERM/SIGINT.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import signal
import sys

import httpx
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from athlete_sweep.parse import AthleteData, parse_all_runs, parse_summary  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
WAF_MARKERS = ("x-amzn-waf", "human verification", "captcha", "just a moment",
               "request unsuccessful")
IPPORT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")
VALIDATE_URL = "https://www.parkrun.org.uk/parkrunner/620/"
VALIDATE_MARK = "(A620)"

SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt",
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt",
    "https://raw.githubusercontent.com/yemixzy/proxy-list/main/proxies/http.txt",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
    "https://proxyspace.pro/http.txt",
]

TARGET = int(os.getenv("PM_FREE_TARGET", "100"))          # желаемое число активных прокси
DELAY = float(os.getenv("PM_FREE_DELAY", "35"))           # задержка между атлетами на прокси
POOL_MAX = int(os.getenv("PM_FREE_DB_POOL", "15"))        # коннектов к БД
# Параллельность валидации держим умеренной: бокс 2-ядерный и делит ресурсы с
# живым сайтом, а живые прокси медленные (5-9с) — при 200-параллели они не
# укладывались в таймаут под контеншеном и пул выходил пустым.
TCP_CONC = int(os.getenv("PM_FREE_TCP_CONC", "400"))        # префильтр дешёвый — можно много
VALIDATE_CONC = int(os.getenv("PM_FREE_VALIDATE_CONC", "50"))
VALIDATE_BATCH = int(os.getenv("PM_FREE_VALIDATE_BATCH", "1500"))  # parkrun-проверка TCP-живых
MAX_CONSEC_ERR = 3                                        # ошибок/капч подряд = отлёжка
# Ступенчатая отлёжка (короче VPN — free эфемерны): 5м,15м,30м,1ч,2ч,6ч.
LADDER = [300, 900, 1800, 3600, 7200, 21600]

_stop = asyncio.Event()


# ───────────────────────── валидация прокси ─────────────────────────
async def _harvest() -> list[str]:
    """Список кандидатов В ПОРЯДКЕ источников (первые в SOURCES — самые «урожайные»,
    их прокси валидируем первыми, не размывая случайной перетасовкой)."""
    seen: set[str] = set()
    ordered: list[str] = []
    async with httpx.AsyncClient(timeout=20) as c:
        for url in SOURCES:
            try:
                r = await c.get(url)
                for m in IPPORT_RE.finditer(r.text):
                    if 0 < int(m.group(2)) < 65536:
                        p = f"{m.group(1)}:{m.group(2)}"
                        if p not in seen:
                            seen.add(p); ordered.append(p)
            except Exception:
                continue
    return ordered


async def _tcp_alive(proxy: str, sem: asyncio.Semaphore) -> str | None:
    """Дешёвый префильтр: открывается ли TCP на ip:port за 3с. Отсекает ~95%
    мёртвых кандидатов почти без нагрузки, чтобы тяжёлую parkrun-проверку делать
    только по живым — так реально просканировать десятки тысяч адресов."""
    host, _, port = proxy.partition(":")
    async with sem:
        try:
            fut = asyncio.open_connection(host, int(port))
            _reader, writer = await asyncio.wait_for(fut, timeout=3)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return proxy
        except Exception:
            return None


async def _validate(proxy: str, sem: asyncio.Semaphore) -> tuple[str, int] | None:
    """Прокси валиден, если отдаёт НАСТОЯЩУЮ страницу parkrun (атлет 620 →
    штрихкод A620): это отсекает и мёртвые прокси, и капчу/заглушки за один
    запрос. Мёртвые отваливаются по таймауту, до parkrun доходят только живые."""
    async with sem:
        try:
            async with httpx.AsyncClient(proxy=f"http://{proxy}", timeout=12,
                                         headers={"User-Agent": UA},
                                         follow_redirects=True) as c:
                r = await c.get(VALIDATE_URL)
                if r.status_code == 200 and VALIDATE_MARK in r.text:
                    return proxy, int(r.elapsed.total_seconds() * 1000)
        except Exception:
            return None
    return None


async def replenish(pool: AsyncConnectionPool) -> None:
    """Добрать новых валидных прокси в липкий пул, если активных < TARGET."""
    async with pool.connection() as conn:
        active = (await (await conn.execute(
            "SELECT count(*) FROM free_proxies WHERE last_ok_at IS NOT NULL "
            "AND (cooldown_until IS NULL OR cooldown_until<=now())")).fetchone())[0]
        known = {r[0] for r in await (await conn.execute("SELECT proxy FROM free_proxies")).fetchall()}
    if active >= TARGET:
        return
    cand = [p for p in await _harvest() if p not in known]
    if not cand:
        return
    # Ступень 1 — дешёвый TCP-префильтр (десятки тысяч адресов за секунды).
    tcp_sem = asyncio.Semaphore(TCP_CONC)
    alive = [r for r in await asyncio.gather(*(_tcp_alive(p, tcp_sem) for p in cand)) if r]
    # Ступень 2 — тяжёлая parkrun-проверка только по TCP-живым, батчем.
    alive = alive[:VALIDATE_BATCH]
    sem = asyncio.Semaphore(VALIDATE_CONC)
    good = [r for r in await asyncio.gather(*(_validate(p, sem) for p in alive)) if r]
    print(f"[replenish] активных {active}/{TARGET}, кандидатов {len(cand)}, "
          f"TCP-живых {len(alive)}, parkrun-живых +{len(good)}", flush=True)
    if good:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO free_proxies (proxy, last_ok_at, latency_ms)
                       VALUES (%s, now(), %s)
                       ON CONFLICT (proxy) DO UPDATE SET last_ok_at=now(),
                         latency_ms=EXCLUDED.latency_ms, fails=0, ban_level=0,
                         cooldown_until=NULL""",
                    [(p, lat) for p, lat in good])
            await conn.commit()


# ───────────────────────── сбор через прокси ─────────────────────────
async def _claim(pool: AsyncConnectionPool, worker: str) -> int | None:
    async with pool.connection() as conn:
        row = await (await conn.execute(
            """UPDATE crawl_queue SET claimed_by=%s, claimed_at=now()
               WHERE athlete_id = (SELECT athlete_id FROM crawl_queue
                   WHERE status='pending'
                     AND (claimed_at IS NULL OR claimed_at < now() - interval '60 min')
                   ORDER BY athlete_id FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING athlete_id""", (worker,))).fetchone()
        await conn.commit()
        return row[0] if row else None


def _classify(status_code: int, headers, body: str) -> str:
    low = body[:2000].lower()
    waf = "x-amzn-waf-action" in {k.lower() for k in headers}
    if status_code in (403, 405) or waf or any(m in low for m in WAF_MARKERS):
        return "protected"
    if status_code == 404:
        return "not_found"
    return "ok"


async def _store(pool: AsyncConnectionPool, aid: int, data: AthleteData, raw: str | None) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO athletes
               (athlete_id,name,barcode,age_category,total_runs,status,parsed_at,source,raw_html)
               VALUES (%s,%s,%s,%s,%s,%s,now(),'crawl',%s)
               ON CONFLICT (athlete_id) DO UPDATE SET
                 name=EXCLUDED.name, barcode=EXCLUDED.barcode, age_category=EXCLUDED.age_category,
                 total_runs=EXCLUDED.total_runs, status=EXCLUDED.status, parsed_at=now(),
                 source='crawl', raw_html=EXCLUDED.raw_html, updated_at=now()""",
            (aid, data.name, data.barcode, data.age_category, data.total_runs, data.status, raw))
        if data.runs:
            await conn.execute("DELETE FROM runs WHERE athlete_id=%s", (aid,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO runs (athlete_id,event_slug,event_name,run_date,run_number,
                       position,finish_time_sec,age_grade) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (athlete_id,event_slug,run_date) DO NOTHING""",
                    [(aid, r["event_slug"], r["event_name"], r["run_date"], r["run_number"],
                      r["position"], r["finish_time_sec"], r["age_grade"]) for r in data.runs])
        if data.volunteer_total:
            await conn.execute(
                "INSERT INTO volunteer_summary (athlete_id,total_credits) VALUES (%s,%s) "
                "ON CONFLICT (athlete_id) DO UPDATE SET total_credits=EXCLUDED.total_credits",
                (aid, data.volunteer_total))
        if data.volunteer_detail:
            await conn.execute("DELETE FROM volunteer_detail WHERE athlete_id=%s", (aid,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO volunteer_detail (athlete_id,role,occasions) VALUES (%s,%s,%s) "
                    "ON CONFLICT (athlete_id,role) DO NOTHING",
                    [(aid, v["role"], v["occasions"]) for v in data.volunteer_detail])
        await conn.commit()


async def _requeue(pool: AsyncConnectionPool, aid: int, err: str | None = None) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE crawl_queue SET status='pending', claimed_by=NULL, "
                           "attempts=attempts+1, error=%s WHERE athlete_id=%s", (err, aid))
        await conn.commit()


async def _record_ban(pool: AsyncConnectionPool, proxy: str) -> None:
    """Бан прокси: НЕ удаляем — ступенчатая отлёжка, ban_level++ (ротация)."""
    async with pool.connection() as conn:
        lvl = (await (await conn.execute(
            "SELECT ban_level FROM free_proxies WHERE proxy=%s", (proxy,))).fetchone())[0]
        cd = LADDER[min(lvl, len(LADDER) - 1)]
        await conn.execute(
            "UPDATE free_proxies SET cooldown_until=now() + (%s || ' seconds')::interval, "
            "ban_level=ban_level+1, last_fail_at=now() WHERE proxy=%s", (cd, proxy))
        await conn.commit()


async def worker(pool: AsyncConnectionPool, proxy: str) -> None:
    """Один прокси: claim→2 страницы→store. При серии капч/ошибок — отлёжка и выход
    (супервайзер поднимет заново после лестницы)."""
    consec = 0
    try:
        async with httpx.AsyncClient(proxy=f"http://{proxy}", timeout=30,
                                     headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
                                     follow_redirects=True) as client:
            while not _stop.is_set():
                aid = await _claim(pool, proxy)
                if aid is None:
                    await asyncio.sleep(30); continue
                base = f"https://www.parkrun.org.uk/parkrunner/{aid}/"
                try:
                    r = await client.get(base)
                    kind = _classify(r.status_code, r.headers, r.text)
                    if kind == "protected":
                        raise _Protected()
                    data = (AthleteData(status="not_found") if kind == "not_found"
                            else parse_summary(r.text, str(aid)))
                    if data.status == "ok":
                        await asyncio.sleep(1 + random.random())
                        r2 = await client.get(base + "all/")
                        if _classify(r2.status_code, r2.headers, r2.text) == "protected":
                            raise _Protected()
                        data.runs = parse_all_runs(r2.text, str(aid))
                    raw = r.text if data.status == "unclassified" else None
                    await _store(pool, aid, data, raw)
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE crawl_queue SET status=%s, claimed_by=NULL, "
                                           "fetched_at=now() WHERE athlete_id=%s", (data.status, aid))
                        await conn.execute("UPDATE free_proxies SET last_ok_at=now(), fails=0, "
                                           "ban_level=0 WHERE proxy=%s", (proxy,))
                        await conn.commit()
                    consec = 0
                except _Protected:
                    await _requeue(pool, aid)
                    consec += 1
                    if consec >= MAX_CONSEC_ERR:
                        await _record_ban(pool, proxy); return
                except Exception as exc:
                    await _requeue(pool, aid, repr(exc)[:200])
                    consec += 1
                    if consec >= MAX_CONSEC_ERR:
                        await _record_ban(pool, proxy); return
                await asyncio.sleep(DELAY * random.uniform(0.85, 1.15))
    except Exception:
        await _record_ban(pool, proxy)


class _Protected(Exception):
    pass


# ───────────────────────── супервайзер ─────────────────────────
async def main() -> None:
    dsn = os.environ["PM_WORLD_DSN"]
    pool = AsyncConnectionPool(dsn, min_size=2, max_size=POOL_MAX, open=False)
    await pool.open()
    print(f"free-сборщик: TARGET={TARGET}, DELAY={DELAY}с, DB-пул={POOL_MAX}", flush=True)
    tasks: dict[str, asyncio.Task] = {}
    last_replenish = 0.0
    loops = 0
    try:
        while not _stop.is_set():
            # снять завершённые
            for p in [p for p, t in tasks.items() if t.done()]:
                del tasks[p]
            # добор новых прокси не чаще раза в ~5 мин
            loops += 1
            if loops == 1 or loops % 10 == 0:
                try:
                    await replenish(pool)
                except Exception as exc:  # noqa: BLE001
                    print(f"[replenish] ошибка: {exc!r}", flush=True)
            # активные прокси (валидные, не в отлёжке), до TARGET
            async with pool.connection() as conn:
                active = [r[0] for r in await (await conn.execute(
                    """SELECT proxy FROM free_proxies
                       WHERE last_ok_at IS NOT NULL
                         AND (cooldown_until IS NULL OR cooldown_until<=now())
                       ORDER BY ban_level, last_ok_at DESC LIMIT %s""", (TARGET,))).fetchall()]
            for proxy in active:
                if proxy not in tasks:
                    tasks[proxy] = asyncio.create_task(worker(pool, proxy))
            if loops % 5 == 0:
                print(f"[sup] активных воркеров {len(tasks)}/{TARGET}", flush=True)
            await asyncio.sleep(30)
    finally:
        _stop.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await pool.close()


def _handle_sig(*_a):
    _stop.set()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, _handle_sig)
    loop.run_until_complete(main())
