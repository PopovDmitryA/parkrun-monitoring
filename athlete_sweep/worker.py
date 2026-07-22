#!/usr/bin/env python3
"""Воркер-краулер атлетов parkrun: claim из crawl_queue → fetch /all через
VPN-прокси → разбор/классификация → запись (коммит на каждой итерации).

Запуск (на сервере, PM_WORLD_DSN в окружении):
  python -m athlete_sweep.worker --worker de --proxy http://127.0.0.1:10811 \
      --limit 500 --delay 6
Параллельно — по одному воркеру на VPN-выход (см. sweep_run.sh).
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timezone

import httpx
import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from athlete_sweep.parse import AthleteData, parse_all_runs, parse_summary  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
WAF_MARKERS = ("x-amzn-waf", "human verification", "captcha", "just a moment",
               "request unsuccessful")
MAX_CONSECUTIVE_PROTECTED = 5


def _now():
    return datetime.now(timezone.utc)


def claim(conn, worker: str, ttl_min: int) -> int | None:
    row = conn.execute(
        """
        UPDATE crawl_queue SET claimed_by=%s, claimed_at=now()
        WHERE athlete_id = (
            SELECT athlete_id FROM crawl_queue
            WHERE status='pending'
              AND (claimed_at IS NULL OR claimed_at < now() - (%s || ' minutes')::interval)
            ORDER BY athlete_id
            FOR UPDATE SKIP LOCKED LIMIT 1
        ) RETURNING athlete_id
        """,
        (worker, str(ttl_min)),
    ).fetchone()
    conn.commit()
    return row[0] if row else None


def fetch(client: httpx.Client, url: str) -> tuple[str, str]:
    """Вернуть (kind, html): kind in ok|not_found|protected."""
    r = client.get(url)
    body = r.text
    low = body[:2000].lower()
    waf_hdr = "x-amzn-waf-action" in {k.lower() for k in r.headers}
    marker = next((m for m in WAF_MARKERS if m in low), None)
    if r.status_code in (403, 405) or waf_hdr or marker:
        print(f"[fetch] protected {url} code={r.status_code} waf_hdr={waf_hdr} "
              f"marker={marker!r} bytes={len(body)} title="
              f"{(next(iter(__import__('re').findall(r'<title[^>]*>([^<]*)', body)), '')[:50])!r}",
              flush=True)
        return "protected", body
    if r.status_code == 404:
        return "not_found", body
    return "ok", body


def store(conn, athlete_id: int, data, raw_html: str | None) -> None:
    conn.execute(
        """INSERT INTO athletes
           (athlete_id,name,barcode,age_category,total_runs,status,parsed_at,source,raw_html)
           VALUES (%s,%s,%s,%s,%s,%s,now(),'crawl',%s)
           ON CONFLICT (athlete_id) DO UPDATE SET
             name=EXCLUDED.name, barcode=EXCLUDED.barcode, age_category=EXCLUDED.age_category,
             total_runs=EXCLUDED.total_runs, status=EXCLUDED.status, parsed_at=now(),
             source='crawl', raw_html=EXCLUDED.raw_html, updated_at=now()""",
        (athlete_id, data.name, data.barcode, data.age_category, data.total_runs,
         data.status, raw_html),
    )
    if data.runs:
        conn.execute("DELETE FROM runs WHERE athlete_id=%s", (athlete_id,))
        conn.cursor().executemany(
            """INSERT INTO runs (athlete_id,event_slug,event_name,run_date,run_number,position,
               finish_time_sec,age_grade) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (athlete_id,event_slug,run_date) DO NOTHING""",
            [(athlete_id, r["event_slug"], r["event_name"], r["run_date"], r["run_number"],
              r["position"], r["finish_time_sec"], r["age_grade"]) for r in data.runs],
        )
    if data.volunteer_total:
        conn.execute(
            "INSERT INTO volunteer_summary (athlete_id,total_credits) VALUES (%s,%s) "
            "ON CONFLICT (athlete_id) DO UPDATE SET total_credits=EXCLUDED.total_credits",
            (athlete_id, data.volunteer_total),
        )
    if data.volunteer_detail:
        conn.execute("DELETE FROM volunteer_detail WHERE athlete_id=%s", (athlete_id,))
        conn.cursor().executemany(
            "INSERT INTO volunteer_detail (athlete_id,role,occasions) VALUES (%s,%s,%s) "
            "ON CONFLICT (athlete_id,role) DO NOTHING",
            [(athlete_id, v["role"], v["occasions"]) for v in data.volunteer_detail],
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", required=True)
    ap.add_argument("--proxy", required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--delay", type=float, default=6.0)
    ap.add_argument("--ttl", type=int, default=60)
    args = ap.parse_args()

    conn = psycopg.connect(os.environ["PM_WORLD_DSN"], autocommit=False)
    client = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
                          proxy=args.proxy, timeout=30.0, follow_redirects=True)
    summary = {"ok": 0, "not_found": 0, "registered_empty": 0, "unclassified": 0, "protected": 0}
    consecutive_protected = 0

    for i in range(args.limit):
        if i:
            time.sleep(args.delay * random.uniform(0.85, 1.15))
        aid = claim(conn, args.worker, args.ttl)
        if aid is None:
            print(f"[{args.worker}] очередь пуста", flush=True)
            break
        base = f"https://www.parkrun.org.uk/parkrunner/{aid}/"

        def requeue(err: str | None = None) -> None:
            conn.execute("UPDATE crawl_queue SET status='pending', claimed_by=NULL, "
                         "attempts=attempts+1, error=%s WHERE athlete_id=%s", (err, aid))
            conn.commit()

        # --- страница 1: summary (имя, классификация, ВОЛОНТЁРСТВО) ---
        try:
            kind, html = fetch(client, base)
        except Exception as exc:
            requeue(repr(exc)[:200]); print(f"[{args.worker}] {aid} сбой: {exc!r}", flush=True); continue
        if kind == "protected":
            consecutive_protected += 1; summary["protected"] += 1; requeue()
            if consecutive_protected >= MAX_CONSECUTIVE_PROTECTED:
                print(f"[{args.worker}] {MAX_CONSECUTIVE_PROTECTED} капч подряд — стоп (WAF)", flush=True); break
            continue
        consecutive_protected = 0
        data = AthleteData(status="not_found") if kind == "not_found" else parse_summary(html, str(aid))

        # --- страница 2: /all (забеги) — только для валидных профилей ---
        if data.status == "ok":
            time.sleep(args.delay * random.uniform(0.85, 1.15))
            try:
                kind2, html2 = fetch(client, base + "all/")
            except Exception as exc:
                requeue(repr(exc)[:200]); print(f"[{args.worker}] {aid} /all сбой: {exc!r}", flush=True); continue
            if kind2 == "protected":
                consecutive_protected += 1; summary["protected"] += 1; requeue()
                if consecutive_protected >= MAX_CONSECUTIVE_PROTECTED:
                    print(f"[{args.worker}] {MAX_CONSECUTIVE_PROTECTED} капч подряд — стоп (WAF)", flush=True); break
                continue
            data.runs = parse_all_runs(html2, str(aid))

        raw = html if data.status == "unclassified" else None
        store(conn, aid, data, raw)
        conn.execute("UPDATE crawl_queue SET status=%s, claimed_by=NULL, fetched_at=now() "
                     "WHERE athlete_id=%s", (data.status, aid))
        conn.commit()  # коммит на КАЖДОЙ итерации
        summary[data.status] = summary.get(data.status, 0) + 1

    print(f"[{args.worker}] итог: {summary}", flush=True)


if __name__ == "__main__":
    main()
