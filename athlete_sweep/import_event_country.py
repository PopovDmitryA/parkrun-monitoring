#!/usr/bin/env python3
"""Импорт маппинга «слаг локации → страна» из monitoring parkrun.db в pm-postgres.

Табло обхода (страница /hq) показывает самую частую локацию атлета и флаг её
страны, но pm-postgres знает только слаги забегов (event_slug), без страны.
Здесь берём страны из каталога monitoring (events.country_code → countries.name/url),
ISO2 вычисляем из домена (parkrun.com.au→au, parkrun.org.uk→gb), и кладём в
таблицу event_country, к которой джойнится API. Запуск на сервере (видит обе БД).
"""
from __future__ import annotations

import os
import pathlib
import sqlite3

import psycopg

MON_DB = pathlib.Path.home() / "parkrun-monitoring" / "data" / "parkrun.db"
SPECIAL = {"uk": "gb"}  # домен .uk → флаг 🇬🇧


def iso2_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tld = url.strip().lower().rstrip("/").split(".")[-1]
    if len(tld) == 2 and tld.isalpha():
        return SPECIAL.get(tld, tld)
    return None


def main() -> None:
    mon = sqlite3.connect(str(MON_DB))
    countries = {c: (name, iso2_from_url(url))
                 for c, name, url in mon.execute("SELECT code, name, url FROM countries")}
    rows = []
    for slug, cc in mon.execute("SELECT eventname, country_code FROM events"):
        name, iso2 = countries.get(cc, (None, None))
        rows.append((slug, cc, name, iso2))
    print(f"событий: {len(rows)}, стран: {len(countries)}", flush=True)

    stg = psycopg.connect(os.environ["PM_WORLD_DSN"])
    stg.execute("""
        CREATE TABLE IF NOT EXISTS event_country (
            slug text PRIMARY KEY,
            country_code int,
            country_name text,
            iso2 text
        )""")
    stg.commit()
    with stg.cursor() as c:
        c.executemany("""
            INSERT INTO event_country (slug, country_code, country_name, iso2)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (slug) DO UPDATE SET
              country_code=EXCLUDED.country_code, country_name=EXCLUDED.country_name,
              iso2=EXCLUDED.iso2""", rows)
    stg.commit()
    n = stg.execute("SELECT count(*) FROM event_country WHERE iso2 IS NOT NULL").fetchone()[0]
    print(f"записано в event_country: {len(rows)} (с iso2: {n})", flush=True)


if __name__ == "__main__":
    main()
