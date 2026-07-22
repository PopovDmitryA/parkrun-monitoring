#!/usr/bin/env python3
"""Перенос уже спарсенных 55k parkrun-атлетов из прод-БД run5k.run в staging
(pm-postgres, parkrun_world). Запускать НА СЕРВЕРЕ (видит прод, легаси, staging,
monitoring-SQLite через localhost).

parsed_at: прод profile_checked_at → иначе легаси parkrun_users.last_updated по
user_id → фолбэк 2025-11-22 (для 14 фиктивных 2026-12-31 и NULL).
is_russian_runner: >=50% забегов на российских локациях (слаги из monitoring,
country_code=79). source='legacy_migration', status='ok'.

Использование: python migrate_from_prod.py [--limit N]  (N — для теста на подвыборке)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sqlite3
import sys
from collections import defaultdict

import psycopg

FALLBACK = dt.datetime(2025, 11, 22, tzinfo=dt.timezone.utc)
LEGACY_DSN = "postgresql://readonly_user:read_my_database@127.0.0.1:5432/five_verst_stats"
MON_DB = pathlib.Path.home() / "parkrun-monitoring" / "data" / "parkrun.db"


def prod_dsn() -> str:
    for line in open("/opt/saturday-runs-next/.env"):
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    sys.exit("прод DATABASE_URL не найден")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="только N участников (для теста)")
    args = ap.parse_args()

    ru_slugs = {r[0] for r in sqlite3.connect(MON_DB).execute(
        "SELECT eventname FROM events WHERE country_code=79")}
    print(f"российских слагов (monitoring): {len(ru_slugs)}", flush=True)

    # легаси last_updated по user_id (фиктивные/NULL пропускаем → фолбэк)
    leg_dates: dict[str, dt.datetime] = {}
    with psycopg.connect(LEGACY_DSN) as leg:
        for uid, lu in leg.execute("SELECT user_id, last_updated FROM parkrun_users"):
            if lu is None or lu.year >= 2026:
                continue
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=dt.timezone.utc)
            leg_dates[str(uid)] = lu
    print(f"легаси-дат подхвачено: {len(leg_dates)}", flush=True)

    prod = psycopg.connect(prod_dsn())
    lim = f"LIMIT {args.limit}" if args.limit else ""

    # 1) участники
    parts = prod.execute(f"""
        SELECT pt.external_user_id, pt.display_name, pt.barcode_id, pt.age_category,
               pt.profile_checked_at
        FROM participants pt JOIN platforms p ON p.id=pt.platform_id
        WHERE p.code='parkrun' ORDER BY pt.external_user_id {lim}""").fetchall()
    keep = {str(r[0]) for r in parts}
    print(f"участников: {len(parts)}", flush=True)

    # 2) забеги (только по отобранным участникам, если тест)
    runs_by: dict[str, list] = defaultdict(list)
    cur = prod.execute("""
        SELECT pt.external_user_id, l.external_key, e.event_date, e.event_number,
               rr.finish_time_sec, rr.position, rr.is_pr
        FROM run_results rr
        JOIN events e ON e.id=rr.event_id
        JOIN locations l ON l.id=e.location_id
        JOIN participants pt ON pt.id=rr.participant_id
        JOIN platforms p ON p.id=pt.platform_id
        WHERE p.code='parkrun'""")
    for uid, slug, edate, enum, tsec, pos, is_pr in cur:
        uid = str(uid)
        if args.limit and uid not in keep:
            continue
        runs_by[uid].append((slug, edate, enum, tsec, pos, is_pr))
    print(f"забегов подхвачено по участникам: {sum(len(v) for v in runs_by.values())}", flush=True)

    # 3) волонтёрство (role → occasions)
    vol_by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cur = prod.execute("""
        SELECT pt.external_user_id, vr.role
        FROM volunteer_results vr
        JOIN participants pt ON pt.id=vr.participant_id
        JOIN events e ON e.id=vr.event_id
        JOIN platforms p ON p.id=e.platform_id
        WHERE p.code='parkrun'""")
    for uid, role in cur:
        uid = str(uid)
        if args.limit and uid not in keep:
            continue
        vol_by[uid][role or "Unknown"] += 1

    # 4) запись в staging
    stg = psycopg.connect(os.environ["PM_WORLD_DSN"])
    a_rows, r_rows, vs_rows, vd_rows = [], [], [], []
    skipped = 0
    for ext, name, barcode, age, checked in parts:
        ext = str(ext)
        if not ext.isdigit():
            skipped += 1
            continue
        aid = int(ext)
        runs = runs_by.get(ext, [])
        total = len(runs)
        ru = sum(1 for slug, *_ in runs if slug in ru_slugs)
        is_ru = total > 0 and ru / total >= 0.5
        parsed_at = checked or leg_dates.get(ext) or FALLBACK
        a_rows.append((aid, name, barcode, age, total, is_ru, "ok", parsed_at, "legacy_migration"))
        for slug, edate, enum, tsec, pos, is_pr in runs:
            r_rows.append((aid, slug, edate, enum, pos, tsec, is_pr))
        vol = vol_by.get(ext)
        if vol:
            vs_rows.append((aid, sum(vol.values())))
            for role, occ in vol.items():
                vd_rows.append((aid, role, occ))

    def batch(conn, sql, rows, n=5000):
        with conn.cursor() as c:
            for i in range(0, len(rows), n):
                c.executemany(sql, rows[i:i + n])
        conn.commit()

    batch(stg, """INSERT INTO athletes
        (athlete_id,name,barcode,age_category,total_runs,is_russian_runner,status,parsed_at,source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (athlete_id) DO NOTHING""", a_rows)
    batch(stg, """INSERT INTO runs
        (athlete_id,event_slug,run_date,run_number,position,finish_time_sec,is_pb)
        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (athlete_id,event_slug,run_date) DO NOTHING""", r_rows)
    batch(stg, "INSERT INTO volunteer_summary (athlete_id,total_credits) VALUES (%s,%s) "
               "ON CONFLICT (athlete_id) DO NOTHING", vs_rows)
    batch(stg, "INSERT INTO volunteer_detail (athlete_id,role,occasions) VALUES (%s,%s,%s) "
               "ON CONFLICT (athlete_id,role) DO NOTHING", vd_rows)

    ru_count = sum(1 for a in a_rows if a[5])
    print(f"\n=== ИТОГ ===", flush=True)
    print(f"атлетов записано: {len(a_rows)} (пропущено нечисловых id: {skipped})")
    print(f"  из них is_russian_runner: {ru_count}")
    print(f"забегов: {len(r_rows)}")
    print(f"волонтёрство: summary {len(vs_rows)}, detail {len(vd_rows)}")


if __name__ == "__main__":
    main()
