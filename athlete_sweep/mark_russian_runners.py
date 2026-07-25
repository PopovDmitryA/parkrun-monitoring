#!/usr/bin/env python3
"""Пересчёт is_russian_runner: ХОТЯ БЫ ОДИН забег в России → true.

Смысл флага (решение Дмитрия, 25.07.2026): по нему база разъедется надвое —
атлеты с хотя бы одной пробежкой в РФ уедут в БД сайта run5k.run, весь остальной
мир останется в большой parkrun-базе рядом. Поэтому порог — 1 забег, а НЕ >=50%,
как считала первоначальная миграция (migrate_from_prod.py) и как было в схеме.

Для новых атлетов флаг ставится на лету в worker.store() (единая точка: manager,
file_parser, mac_sweep_worker). Этот скрипт — разовый/периодический бэкфилл для
уже лежащих в базе, включая перещёт мигрированных 55k со старым порогом.

Матч РФ: runs.event_slug делается из НАЗВАНИЯ (с дефисами), канонический
parkrun-слаг — без разделителей, поэтому джойн со снятием не-алфанумерики
(иначе матч ~40% вместо ~91%). Справочник — event_country (iso2='ru', 110 событий).

Запуск на сервере:
  cd /home/viewer/parkrun-monitoring && set -a && . ./.env && set +a \
    && .venv/bin/python -m athlete_sweep.mark_russian_runners
Опции: --dry-run (только посчитать), --source crawl|legacy_migration|all
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

RU_EXISTS = """EXISTS (
    SELECT 1 FROM runs r
    JOIN event_country ec
      ON ec.slug = regexp_replace(r.event_slug, '[^a-z0-9]', '', 'g')
    WHERE r.athlete_id = a.athlete_id AND lower(ec.iso2) = 'ru')"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Пересчёт is_russian_runner (>=1 забег в РФ)")
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не писать")
    ap.add_argument("--source", default="all",
                    choices=["all", "crawl", "legacy_migration"],
                    help="кого пересчитывать")
    args = ap.parse_args()

    dsn = os.getenv("PM_WORLD_DSN")
    if not dsn:
        sys.exit("нет PM_WORLD_DSN в окружении")

    src_filter = "" if args.source == "all" else f" AND a.source = '{args.source}'"

    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        # Санити: справочник РФ на месте? Пустой → все флаги обнулились бы.
        ru_events = cur.execute(
            "SELECT count(*) FROM event_country WHERE lower(iso2)='ru'").fetchone()[0]
        print(f"РФ-событий в справочнике event_country: {ru_events}")
        if ru_events == 0:
            sys.exit("справочник пуст — сначала import_event_country.py, иначе затрём флаги")

        before = cur.execute(
            "SELECT count(*) FILTER (WHERE is_russian_runner), count(*) FROM athletes").fetchone()
        print(f"сейчас помечено: {before[0]} из {before[1]} атлетов")

        should = cur.execute(
            f"SELECT count(*) FROM athletes a WHERE {RU_EXISTS}{src_filter}").fetchone()[0]
        print(f"должно быть по новому правилу (>=1 забег в РФ){' для ' + args.source if args.source != 'all' else ''}: {should}")

        if args.dry_run:
            print("--dry-run: ничего не пишу.")
            return

        # Пишем только там, где значение реально меняется — не трогаем лишние строки.
        cur.execute(f"""
            UPDATE athletes a
               SET is_russian_runner = {RU_EXISTS}, updated_at = now()
             WHERE is_russian_runner IS DISTINCT FROM {RU_EXISTS}{src_filter}""")
        changed = cur.rowcount
        conn.commit()

        after = cur.execute(
            "SELECT count(*) FILTER (WHERE is_russian_runner), count(*) FROM athletes").fetchone()
        print(f"обновлено строк: {changed}")
        print(f"стало помечено: {after[0]} из {after[1]} атлетов")

        by_src = cur.execute("""
            SELECT source, count(*) FILTER (WHERE is_russian_runner), count(*)
              FROM athletes GROUP BY source ORDER BY 2 DESC""").fetchall()
        print("разрез по источнику (россияне / всего):")
        for s, ru, tot in by_src:
            print(f"  {s:<18} {ru} / {tot}")


if __name__ == "__main__":
    main()
