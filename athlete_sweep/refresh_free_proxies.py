#!/usr/bin/env python3
"""Самообновляющийся пул бесплатных публичных HTTP-прокси для обхода атлетов.

Публичные free-proxy живут недолго, но их тысячи и списки обновляются. Скрипт:
  1) качает несколько публичных списков,
  2) валидирует каждый РЕАЛЬНЫМ запросом parkrun (атлет 620 → должен вернуть
     штрихкод «(A620)»; это отсекает и мёртвые прокси, и капчу/заглушки),
  3) живые апсертит в sweep_exits как account='free' (name=free-<ip>-<port>),
  4) те free-выходы, что больше не проходят валидацию, — enabled=false.

Менеджер держит на аккаунт 'free' отдельный (большой) лимит потоков; сдохший
прокси он сам уводит в короткий cooldown по серии ошибок. Запуск по крону
(каждые ~20 мин) на сервере, PM_WORLD_DSN в окружении.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import re

import httpx
import psycopg

LISTS = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
VALIDATE_URL = "https://www.parkrun.org.uk/parkrunner/620/"
VALIDATE_MARK = "(A620)"          # штрихкод настоящего профиля основателя parkrun
IPPORT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")
MAX_CANDIDATES = int(os.getenv("PM_FREE_CANDIDATES", "900"))
PROBE_WORKERS = int(os.getenv("PM_FREE_PROBE_WORKERS", "80"))
# Длинная задержка на free-прокси: каждый делает мало запросов → дольше живёт
# и реже ловит капчу; объём добираем ЧИСЛОМ прокси в пуле, а не разгоном одного.
FREE_DELAY = float(os.getenv("PM_FREE_DELAY", "35"))
FREE_FLOOR = float(os.getenv("PM_FREE_FLOOR", "25"))


def harvest() -> list[str]:
    seen: set[str] = set()
    for url in LISTS:
        try:
            r = httpx.get(url, timeout=20)
            for m in IPPORT_RE.finditer(r.text):
                port = int(m.group(2))
                if 0 < port < 65536:
                    seen.add(f"{m.group(1)}:{m.group(2)}")
        except Exception:
            continue
    cand = list(seen)
    # детерминированное «перемешивание» без random (стабильно между запусками)
    cand.sort(key=lambda s: hash(s) & 0xFFFFFFFF)
    return cand[:MAX_CANDIDATES]


def probe(proxy: str) -> tuple[str, float] | None:
    """Вернуть (proxy, latency) если прокси отдаёт РЕАЛЬНУЮ страницу parkrun."""
    try:
        with httpx.Client(proxy=f"http://{proxy}", timeout=9.0,
                          headers={"User-Agent": UA}) as c:
            r = c.get(VALIDATE_URL)
            if r.status_code == 200 and VALIDATE_MARK in r.text:
                return proxy, r.elapsed.total_seconds()
    except Exception:
        return None
    return None


def main() -> None:
    cand = harvest()
    print(f"кандидатов: {len(cand)}", flush=True)
    good: list[tuple[str, float]] = []
    with cf.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        for res in ex.map(probe, cand):
            if res:
                good.append(res)
    good.sort(key=lambda x: x[1])
    print(f"живых (реальные данные): {len(good)}", flush=True)
    for p, t in good:
        print(f"  {p}  {t:.2f}s", flush=True)

    conn = psycopg.connect(os.environ["PM_WORLD_DSN"], autocommit=True)
    valid_names = set()
    for proxy, _lat in good:
        name = "free-" + proxy.replace(".", "-").replace(":", "-")
        valid_names.add(name)
        conn.execute(
            """INSERT INTO sweep_exits (name, proxy, kind, account, enabled,
                   delay_sec, delay_floor, cooldown_until, ban_level)
               VALUES (%s, %s, 'http', 'free', true, %s, %s, NULL, 0)
               ON CONFLICT (name) DO UPDATE SET
                   proxy=EXCLUDED.proxy, enabled=true, delay_sec=EXCLUDED.delay_sec,
                   delay_floor=EXCLUDED.delay_floor, cooldown_until=NULL, ban_level=0""",
            (name, f"http://{proxy}", FREE_DELAY, FREE_FLOOR),
        )
    # ретайр тех free-выходов, что больше не валидны
    rows = conn.execute("SELECT name FROM sweep_exits WHERE account='free'").fetchall()
    retired = 0
    for (name,) in rows:
        if name not in valid_names:
            conn.execute("UPDATE sweep_exits SET enabled=false WHERE name=%s", (name,))
            retired += 1
    print(f"апсерт: {len(valid_names)} активных free-выходов, ретайр {retired}", flush=True)


if __name__ == "__main__":
    main()
