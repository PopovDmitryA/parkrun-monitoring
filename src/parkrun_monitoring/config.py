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

# events.json carries country codes and domains but no country names, so the
# names are kept here. Code 0 is parkrun's pseudo-country for worldwide
# totals. Codes 31 and 79 belong to countries that have left parkrun.
COUNTRY_NAMES: dict[int, str] = {
    0: "Worldwide",
    3: "Australia",
    4: "Austria",
    14: "Canada",
    23: "Denmark",
    30: "Finland",
    31: "France",
    32: "Germany",
    42: "Ireland",
    44: "Italy",
    46: "Japan",
    54: "Lithuania",
    57: "Malaysia",
    64: "Netherlands",
    65: "New Zealand",
    67: "Norway",
    74: "Poland",
    79: "Russia",
    82: "Singapore",
    85: "South Africa",
    88: "Sweden",
    97: "United Kingdom",
    98: "United States",
}


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
    worker_delay: float
    claim_ttl_minutes: int
    claim_command: str | None


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
        # Queue workers pace slower than the interactive history walk: several
        # of them run at once, so each one alone stays far below any rate that
        # could look automated.
        worker_delay=float(os.getenv("PM_WORKER_DELAY", "40")),
        claim_ttl_minutes=int(os.getenv("PM_CLAIM_TTL_MIN", "60")),
        claim_command=os.getenv("PM_CLAIM_COMMAND") or None,
    )
