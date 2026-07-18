"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EVENTS_JSON_URL = "https://images.parkrun.com/events.json"
COUNTRY_CHART_URL = (
    "https://results-service.parkrun.com/resultsSystem/App/globalChartNumRunnersAndEvents.php"
)

# Pseudo-code used by the chart endpoint for worldwide totals.
GLOBAL_COUNTRY_CODE = 0

# Countries that left parkrun and are gone from events.json, but whose weekly
# statistics are still served by the chart endpoint.
KNOWN_CLOSED_COUNTRIES: dict[int, str] = {
    31: "www.parkrun.fr",
    79: "www.parkrun.ru",
}

SERIES_NAMES = {1: "parkrun (5k)", 2: "junior parkrun (2k)"}


@dataclass(frozen=True)
class Config:
    db_path: Path
    user_agent: str
    request_delay: float
    vk_token: str | None
    vk_peer_id: int | None
    gate_command: str | None
    push_command: str | None
    history_delay: float


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    peer_raw = os.getenv("VK_PEER_ID", "").strip()
    return Config(
        db_path=Path(os.getenv("PM_DB_PATH") or PROJECT_ROOT / "data" / "parkrun.db"),
        # The results-service endpoint returns 403 to non-browser user agents,
        # so a browser-like UA is the working default.
        user_agent=os.getenv(
            "PM_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        ),
        request_delay=float(os.getenv("PM_REQUEST_DELAY", "3")),
        vk_token=os.getenv("VK_TOKEN") or None,
        vk_peer_id=int(peer_raw) if peer_raw else None,
        gate_command=os.getenv("PM_GATE_COMMAND") or None,
        push_command=os.getenv("PM_PUSH_COMMAND") or None,
        history_delay=float(os.getenv("PM_HISTORY_DELAY", "30")),
    )
