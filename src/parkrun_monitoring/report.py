"""Periodic status report for the collector workers, delivered to VK.

Summarises the last N hours of ``worker_runs`` plus overall history
progress; sent by cron so a silent collector is just as visible as a
working one.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

from . import claims
from .config import Config


def build_sweep_report() -> str:
    """Детальный VK-отчёт по мировому обходу атлетов: прогресс + строка на каждый
    VPN-выход (флаг, состояние/отлёжка, задержка). Пусто, если PM_WORLD_DSN нет."""
    dsn = os.getenv("PM_WORLD_DSN")
    if not dsn:
        return ""
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=5) as conn:
            by = dict(conn.execute("SELECT status, count(*) FROM crawl_queue GROUP BY status").fetchall())
            crawled = conn.execute("SELECT count(*) FROM athletes WHERE source='crawl'").fetchone()[0]
            runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            exits = conn.execute(
                """SELECT name, delay_sec, ban_level,
                          GREATEST(0, EXTRACT(EPOCH FROM (cooldown_until - now())) / 60) AS cd_min
                   FROM sweep_exits WHERE enabled
                   ORDER BY (cooldown_until > now()), name"""
            ).fetchall()
        total = sum(by.values())
        pending = by.get("pending", 0)
        free_gb = shutil.disk_usage("/").free // (1024 ** 3)
        working = sum(1 for _, _, _, cd in exits if cd <= 0)

        lines = ["🌍 Обход атлетов parkrun",
                 f"📊 пройдено {total - pending:,}/{total:,}, собрано {crawled:,} атлетов, "
                 f"{runs:,} забегов",
                 f"🔌 выходов рабочих {working}/{len(exits)}, 💾 диск {free_gb} ГБ"]
        if by.get("unclassified"):
            lines.append(f"⚠️ на ревью: {by['unclassified']}")
        lines.append("")
        for name, delay, bl, cd_min in exits:
            flag = _flag(name)
            if cd_min > 0:
                h, m = divmod(int(cd_min), 60)
                left = f"{h}ч{m:02d}м" if h else f"{m}м"
                lines.append(f"{flag} {name}: 💤 отлёжка {left} (ур.{bl}, {delay:.0f}с)")
            else:
                lines.append(f"{flag} {name}: ✅ работает ({delay:.0f}с)")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — отчёт best-effort
        return f"🌍 Обход атлетов: отчёт недоступен ({exc!r})"[:120]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(worker: str) -> str:
    """Флаг-эмодзи по имени воркера: первые две буквы = код страны, из них
    строим regional-indicator эмодзи (de → 🇩🇪, it → 🇮🇹). 'mac' — особый."""
    if worker.startswith("mac"):
        return "💻"
    m = re.match(r"([a-z])([a-z])", worker)
    if not m:
        return "🏳️"
    return chr(0x1F1E6 + ord(m.group(1)) - 97) + chr(0x1F1E6 + ord(m.group(2)) - 97)


def build_status_report(
    conn: sqlite3.Connection, config: Config, hours: int = 3
) -> str:
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(hours=hours))

    per_worker = conn.execute(
        """
        SELECT worker, COUNT(*) AS runs, SUM(synced) AS synced, SUM(rows) AS rows,
               SUM(failed) AS failed,
               SUM(status = 'aborted') AS aborted,
               SUM(status = 'running') AS running,
               MAX(COALESCE(finished_at, started_at)) AS last_seen
        FROM worker_runs WHERE started_at >= ?
        GROUP BY worker ORDER BY worker
        """,
        (cutoff,),
    ).fetchall()

    progress = conn.execute(
        """
        SELECT SUM(history_synced_at IS NOT NULL), COUNT(*),
               MIN(history_synced_at)
        FROM events WHERE is_active = 1
        """
    ).fetchone()
    history_total = conn.execute("SELECT COUNT(*) FROM event_history").fetchone()[0]
    active = claims.active_claims(conn, config.claim_ttl_minutes)

    lines = [f"parkrun-monitoring: сбор локаций за {hours}ч"]
    if not per_worker:
        lines.append("😴 Воркеры не запускались")
    for w in per_worker:
        flags = []
        if w["aborted"]:
            flags.append(f"аборт×{w['aborted']}")
        if w["running"]:
            flags.append("работает сейчас")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"• {_flag(w['worker'])} {w['worker']}: {w['synced'] or 0} локаций, "
            f"+{w['rows'] or 0} строк, фейлов {w['failed'] or 0}{suffix}"
        )
    if active:
        lines.append(f"🔒 В работе сейчас: {len(active)}")
    synced, total = progress[0] or 0, progress[1] or 0
    remaining = total - synced
    lines.append(
        f"📊 Прогресс: {synced}/{total} локаций пройдено, "
        f"{history_total} строк истории всего"
    )
    if remaining:
        lines.append(f"🆕 Осталось впервые пройти: {remaining}")
    elif config.first_pass_only:
        lines.append("✅ Все локации пройдены впервые — коллектор ждёт новых задач")
    else:
        lines.append("✅ Все локации пройдены — идёт обновление истории")
    if progress[2]:
        lines.append(f"⏳ Самый старый проход: {progress[2][:10]}")
    return "\n".join(lines)
