#!/usr/bin/env python3
"""Сбор описаний parkrun-событий: главная страница + /course/ каждой локации.

Что собираем (структура одинакова на всех языках, включая японский):
- главная, div.homeleft — 8 блоков h4 в фиксированном порядке: что это, когда,
  где, стоимость, темп, волонтёрство, безопасность, «мы дружелюбные»;
- /course/, div.courseleft + div.courseright — интро трассы, карта (iframe),
  описание трассы, удобства, как добраться (с подблоками), кофе после.

Канонические ключи назначаем ТОЛЬКО там, где это надёжно без знания языка:
на главной — по позиции (порядок блоков одинаков во всех странах), на course —
двум якорям (первый блок левой колонки = интро, первый правой = карта).
Остальные блоки сохраняются упорядоченно с canonical_key IS NULL — их можно
канонизировать позже словарём заголовков по уже собранным данным.

Механика доступа — как в waf_solver --fast: страницы качает httpx, браузер
поднимается только за aws-waf-token. ВАЖНО: токен домен-скоуповый, а у каждой
страны свой домен (parkrun.pl, parkrun.jp, …) — обход сгруппирован по доменам,
токен добывается на домен и живёт, пока WAF не попросит новый.

Каталог событий — data/parkrun.db (SQLite parkrun-monitoring), пишем в
pm-postgres (тоннель поднимается сам, как в waf_solver).

Запуск (Mac): пункт 5 в `make parkrun`, или напрямую:
  python -m athlete_sweep.event_pages --delay 2
"""
from __future__ import annotations

import argparse
import gzip
import os
import pathlib
import re
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_DB = os.path.join(ROOT, "data", "parkrun.db")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Порядок h4-блоков главной одинаков во всех странах — ключ по позиции.
HOME_KEYS = ["what_is", "when", "where", "cost", "pace",
             "volunteer", "safeguarding", "friendly"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_pages (
    slug         TEXT NOT NULL,
    page         TEXT NOT NULL,
    url          TEXT NOT NULL,
    http_status  INTEGER,
    lang         TEXT,
    og_image     TEXT,
    raw_gzip     BYTEA,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (slug, page)
);
CREATE TABLE IF NOT EXISTS event_sections (
    slug          TEXT NOT NULL,
    page          TEXT NOT NULL,
    area          TEXT,
    position      INTEGER NOT NULL,
    heading_level INTEGER,
    heading       TEXT,
    canonical_key TEXT,
    content_html  TEXT,
    content_text  TEXT,
    PRIMARY KEY (slug, page, position)
);
CREATE INDEX IF NOT EXISTS ix_event_sections_key ON event_sections (canonical_key);
"""


# ---------------------------------------------------------------- каталог

def load_catalog() -> list[dict]:
    """Активные события активных стран: slug, домен, страна, серия."""
    if not os.path.exists(CATALOG_DB):
        raise SystemExit(f"каталог не найден: {CATALOG_DB}")
    con = sqlite3.connect(CATALOG_DB)
    rows = con.execute("""
        SELECT e.eventname, e.long_name, e.series_id, c.name, c.url
        FROM events e JOIN countries c ON c.code = e.country_code
        WHERE e.is_active = 1 AND c.is_active = 1 AND c.url IS NOT NULL AND c.url <> ''
        ORDER BY c.url, e.eventname""").fetchall()
    con.close()
    return [{"slug": r[0], "name": r[1], "series": r[2], "country": r[3],
             "domain": r[4]} for r in rows]


# ---------------------------------------------------------------- парсер

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_sections(html: str) -> tuple[list[dict], str | None, str | None]:
    """Секции контентных колонок + lang + og:image.

    Секция = заголовок h2/h3/h4 и всё до следующего заголовка того же контейнера.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    lang = (soup.html.get("lang") if soup.html else None) or None
    og = soup.find("meta", attrs={"property": "og:image"})
    og_image = og.get("content") if og else None

    sections: list[dict] = []
    pos = 0
    for area, cls in (("left", ("homeleft", "courseleft")),
                      ("right", ("homeright", "courseright"))):
        div = None
        for c in cls:
            div = soup.find("div", class_=c)
            if div is not None:
                break
        if div is None:
            continue
        # правую колонку главной (виджеты волонтёров/статистики) не сохраняем —
        # описания там нет; правую course-колонку (карта и описание трассы) — да
        if div.get("class") and "homeright" in div.get("class"):
            continue
        heads = div.find_all(["h2", "h3", "h4"])
        for h in heads:
            body_html: list[str] = []
            for sib in h.next_siblings:
                if getattr(sib, "name", None) in ("h2", "h3", "h4"):
                    break
                body_html.append(str(sib))
            raw = "".join(body_html).strip()
            frag = BeautifulSoup(raw, "html.parser")
            text = _clean(frag.get_text(" ", strip=True))
            heading = _clean(h.get_text(" ", strip=True))
            if not heading and not text:
                continue
            sections.append({
                "area": area, "position": pos,
                "heading_level": int(h.name[1]), "heading": heading,
                "canonical_key": None, "content_html": raw, "content_text": text,
            })
            pos += 1
    return sections, lang, og_image


def assign_canonical(page: str, sections: list[dict]) -> None:
    if page == "home":
        # порядок h4 фиксированный во всех языках
        for i, s in enumerate(s for s in sections if s["area"] == "left"):
            if i < len(HOME_KEYS):
                s["canonical_key"] = HOME_KEYS[i]
    else:
        left = [s for s in sections if s["area"] == "left"]
        right = [s for s in sections if s["area"] == "right"]
        if left:
            left[0]["canonical_key"] = "course_intro"
        if right:
            right[0]["canonical_key"] = "course_map"


# ---------------------------------------------------------------- фетч

def is_protected(status: int, headers, body: str) -> bool:
    low = body[:2000].lower()
    if "x-amzn-waf-action" in {k.lower() for k in headers}:
        return True
    return status in (403, 405) or "human verification" in low or "choose all" in low


class TokenHarvester:
    """Браузер за aws-waf-token: лениво грузит CLIP, решает капчу.

    Гонка by design: после решённой капчи страница сама перенавигируется, и
    чтение контента в этот момент бросает исключение — это признак УСПЕХА,
    после него надо просто проверить куки.
    """

    def __init__(self) -> None:
        self._clip = None
        self._rn = None
        self._p = None

    def _ensure(self):
        from athlete_sweep.waf_solver import Clip, next_round_no
        if self._clip is None:
            print("  загружаю CLIP (первая капча)…", flush=True)
            self._clip = Clip()
            self._rn = next_round_no()
        if self._p is None:
            from playwright.sync_api import sync_playwright
            self._p = sync_playwright().start()
        return self._p

    def close(self) -> None:
        if self._p is not None:
            try:
                self._p.stop()
            except Exception:
                pass
            self._p = None

    def harvest(self, url: str) -> str | None:
        from athlete_sweep.waf_solver import _wait_for, pass_captcha

        p = self._ensure()
        br = p.chromium.launch(headless=True, args=["--no-proxy-server"])
        ctx = br.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        token = None
        try:
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            _wait_for(pg, lambda: ("Human Verification" in pg.content()
                                   or "Choose all" in pg.content()
                                   or len(pg.content()) > 25000), timeout=25)
            html = ""
            for _ in range(10):
                try:
                    html = pg.content()
                    break
                except Exception:
                    time.sleep(1)
            if "Human Verification" in html or "Choose all" in html:
                print("  капча — решаю…", flush=True)
                try:
                    ok, _n, self._rn = pass_captcha(pg, self._clip, self._rn)
                    if not ok:
                        return None
                except Exception:
                    pass  # гонка = страница уже уехала после успеха; проверяем куки
                time.sleep(3)
            for _ in range(20):
                for c in ctx.cookies():
                    if c["name"] == "aws-waf-token":
                        token = c["value"]
                if token:
                    break
                time.sleep(1)
        except Exception as exc:
            print(f"  токен не добыт: {exc!r}", flush=True)
        finally:
            try:
                ctx.close()
            except Exception as exc:
                print(f"  контекст браузера не закрылся: {exc!r}", flush=True)
            try:
                br.close()
            except Exception as exc:
                print(f"  !! браузер НЕ закрылся: {exc!r}", flush=True)
        return token


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Сбор описаний parkrun-событий")
    ap.add_argument("--delay", type=float, default=2.0, help="пауза между страницами, сек")
    ap.add_argument("--limit", type=int, default=0, help="сколько событий (0 = все)")
    ap.add_argument("--only", default="", help="один слаг (отладка)")
    ap.add_argument("--refetch", action="store_true",
                    help="перекачивать уже собранные (иначе они пропускаются)")
    ap.add_argument("--dsn", default="", help="DSN pm-postgres (иначе тоннель сам)")
    args = ap.parse_args()

    import httpx
    import psycopg

    events = load_catalog()
    if args.only:
        events = [e for e in events if e["slug"] == args.only]
        if not events:
            raise SystemExit(f"слаг {args.only!r} не найден в каталоге")
    print(f"каталог: {len(events)} активных событий, "
          f"{len({e['domain'] for e in events})} доменов", flush=True)

    # --- тоннель к pm-postgres (как в waf_solver) ---
    tun = None
    dsn = args.dsn
    if not dsn:
        from sshtunnel import SSHTunnelForwarder

        from athlete_sweep.waf_solver import _cred_env
        host = _cred_env("PM_SSH_HOST", "TEMP_SSH_HOST", "PROD_SSH_HOST",
                         default="195.58.34.112")
        user = _cred_env("PM_SSH_USER", "TEMP_SSH_USER", "PROD_SSH_USER", default="viewer")
        pwd = _cred_env("PM_SSH_PASS", "TEMP_SSH_PASSWORD")
        if not pwd:
            from getpass import getpass
            pwd = getpass("SSH-пароль сервера: ")
        import socket as _s
        s = _s.socket(); s.bind(("127.0.0.1", 0)); lp = s.getsockname()[1]; s.close()

        def build():
            return SSHTunnelForwarder(
                (host, 22), ssh_username=user, ssh_password=pwd,
                remote_bind_addresses=[("127.0.0.1", 5433)],
                local_bind_addresses=[("127.0.0.1", lp)], set_keepalive=15.0)

        print(f"поднимаю тоннель к {user}@{host}…", flush=True)
        tun = build()
        tun.start()
        dsn = f"postgresql://parkrun:parkrun_world_local@127.0.0.1:{lp}/parkrun_world"

    conn = psycopg.connect(dsn, autocommit=False, connect_timeout=10)

    def db(fn):
        nonlocal conn
        try:
            return fn(conn)
        except psycopg.OperationalError:
            try:
                conn.close()
            except Exception:
                pass
            for _ in range(12):
                try:
                    if tun is not None and not tun.is_active:
                        tun.restart()
                except Exception:
                    pass
                try:
                    conn = psycopg.connect(dsn, autocommit=False, connect_timeout=10)
                    print("    БД: переподключился", flush=True)
                    return fn(conn)
                except psycopg.OperationalError:
                    time.sleep(5)
            raise RuntimeError("БД недоступна больше минуты")

    db(lambda c: (c.execute(SCHEMA), c.commit()))

    done_set: set[str] = set()
    if not args.refetch:
        rows = db(lambda c: c.execute(
            "SELECT slug FROM event_pages WHERE http_status = 200 "
            "GROUP BY slug HAVING count(*) = 2").fetchall())
        done_set = {r[0] for r in rows}
        if done_set:
            print(f"уже собрано: {len(done_set)} событий — пропускаю", flush=True)

    harvester = TokenHarvester()
    tokens: dict[str, str] = {}  # домен → aws-waf-token
    cli = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
                       timeout=30, follow_redirects=True)

    def fetch_page(domain: str, url: str) -> tuple[int, str] | None:
        """GET с токеном домена; при WAF — новый токен и повтор (до 3 заходов:
        харвест иногда не выдаёт токен с первого раза даже без капчи)."""
        for attempt in (1, 2, 3):
            cookies = {"aws-waf-token": tokens[domain]} if domain in tokens else {}
            try:
                r = cli.get(url, cookies=cookies)
            except httpx.HTTPError as exc:
                print(f"  сеть: {type(exc).__name__} — повтор через 15с", flush=True)
                time.sleep(15)
                continue
            if not is_protected(r.status_code, r.headers, r.text):
                return r.status_code, r.text
            if attempt < 3:
                print(f"  WAF на {domain} — добываю токен…", flush=True)
                token = harvester.harvest(url)
                if token:
                    tokens[domain] = token
                continue
            return None
        return None

    def store(ev: dict, page: str, url: str, status: int, html: str) -> int:
        sections, lang, og_image = parse_sections(html)
        assign_canonical(page, sections)
        raw = gzip.compress(html.encode("utf-8"))

        def _write(c):
            c.execute("""
                INSERT INTO event_pages (slug, page, url, http_status, lang, og_image, raw_gzip)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug, page) DO UPDATE SET url = EXCLUDED.url,
                    http_status = EXCLUDED.http_status, lang = EXCLUDED.lang,
                    og_image = EXCLUDED.og_image, raw_gzip = EXCLUDED.raw_gzip,
                    fetched_at = now()""",
                (ev["slug"], page, url, status, lang, og_image, raw))
            c.execute("DELETE FROM event_sections WHERE slug = %s AND page = %s",
                      (ev["slug"], page))
            for s in sections:
                c.execute("""
                    INSERT INTO event_sections (slug, page, area, position, heading_level,
                                                heading, canonical_key, content_html, content_text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (ev["slug"], page, s["area"], s["position"], s["heading_level"],
                     s["heading"], s["canonical_key"], s["content_html"], s["content_text"]))
            c.commit()

        db(_write)
        return len(sections)

    total = 0
    ok_events = 0
    failed: list[str] = []
    t0 = time.time()
    try:
        for ev in events:
            if ev["slug"] in done_set:
                continue
            if args.limit and total >= args.limit:
                print(f"лимит {args.limit} достигнут", flush=True)
                break
            total += 1
            base = f"https://{ev['domain']}/{ev['slug']}/"
            n_home = n_course = "-"
            got_all = True
            for page, url in (("home", base), ("course", base + "course/")):
                res = fetch_page(ev["domain"], url)
                if res is None:
                    print(f"  {ev['slug']}/{page}: НЕ добыл (WAF)", flush=True)
                    got_all = False
                    break
                status, html = res
                if status == 200:
                    n = store(ev, page, url, status, html)
                    if page == "home":
                        n_home = n
                    else:
                        n_course = n
                else:
                    # 404 и прочее тоже фиксируем — чтобы видеть и не перекачивать
                    db(lambda c, _u=url, _p=page, _s=status: (c.execute(
                        """INSERT INTO event_pages (slug, page, url, http_status)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (slug, page) DO UPDATE SET
                               http_status = EXCLUDED.http_status, fetched_at = now()""",
                        (ev["slug"], _p, _u, _s)), c.commit()))
                time.sleep(args.delay)
            if got_all:
                ok_events += 1
                el = time.time() - t0
                print(f"#{total} {ev['slug']} ({ev['country']}): "
                      f"home {n_home} секц · course {n_course} секц "
                      f"[{el/max(total,1):.1f}с/событие]", flush=True)
            else:
                failed.append(ev["slug"])
    except KeyboardInterrupt:
        print("\nостановлено.", flush=True)
    finally:
        harvester.close()
        try:
            cli.close(); conn.close()
        except Exception:
            pass
        if tun is not None:
            try:
                tun.stop()
            except Exception:
                pass

    print("=" * 46)
    print(f"событий обработано: {ok_events} из {total} за {(time.time()-t0)/60:.1f} мин")
    if failed:
        print(f"не добыто ({len(failed)}): {', '.join(failed[:20])}"
              + (" …" if len(failed) > 20 else ""))
    print("повторный запуск докачает пропущенное (уже собранное пропускается).")


if __name__ == "__main__":
    main()
