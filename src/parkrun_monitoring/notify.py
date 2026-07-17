"""Change notifications: VK message if configured, stdout otherwise."""

from __future__ import annotations

import random

import httpx

from .config import Config
from .sync import ChangeSet

VK_API_URL = "https://api.vk.com/method/messages.send"
VK_API_VERSION = "5.199"
VK_MESSAGE_LIMIT = 4096
MAX_LISTED = 30


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_LISTED])
    rest = len(names) - MAX_LISTED
    return shown + (f" … и ещё {rest}" if rest > 0 else "")


def format_message(changes: ChangeSet) -> str | None:
    """Build a notification text, or None when there is nothing to report."""
    if changes.initial_import:
        return (
            f"parkrun-monitoring: первичный импорт каталога — "
            f"{changes.events_total} событий. Недельная статистика: "
            f"+{changes.stats_new_rows} строк по {changes.stats_countries} странам."
        )
    if not changes.has_catalogue_changes and not changes.stats_failed_countries:
        return None

    lines = ["parkrun-monitoring: изменения в каталоге parkrun"]
    if changes.added:
        names = [f"{e.eventname} ({e.long_name})" for e in changes.added]
        lines.append(f"➕ Появились ({len(names)}): {_listed(names)}")
    if changes.removed:
        names = [f"{r['eventname']} ({r['long_name']})" for r in changes.removed]
        lines.append(f"➖ Пропали ({len(names)}): {_listed(names)}")
    if changes.reappeared:
        names = [e.eventname for e in changes.reappeared]
        lines.append(f"↩️ Вернулись ({len(names)}): {_listed(names)}")
    if changes.modified:
        parts = [
            f"{e.eventname}: {', '.join(diff.keys())}" for e, diff in changes.modified
        ]
        lines.append(f"✏️ Изменены ({len(parts)}): {_listed(parts)}")
    if changes.stats_failed_countries:
        codes = ", ".join(map(str, changes.stats_failed_countries))
        lines.append(f"⚠️ Не удалось обновить статистику стран: {codes}")
    lines.append(f"Всего активных событий: {changes.events_total}")
    return "\n".join(lines)


def send(config: Config, text: str) -> bool:
    """Send the text to VK; fall back to stdout when VK is unavailable.

    A failed notification must never fail the sync itself, so network and
    API errors are reported to stdout instead of being raised.
    """
    if not config.vk_token or not config.vk_peer_id:
        print(text)
        return False
    try:
        return _send_vk(config, text)
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        print(f"[notify] VK delivery failed: {exc!r}")
        print(text)
        return False


def _send_vk(config: Config, text: str) -> bool:
    response = httpx.post(
        VK_API_URL,
        data={
            "access_token": config.vk_token,
            "v": VK_API_VERSION,
            "peer_id": config.vk_peer_id,
            "random_id": random.randint(1, 2_000_000_000),
            "message": text[:VK_MESSAGE_LIMIT],
        },
        timeout=30.0,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"VK API error: {body['error']}")
    return True
