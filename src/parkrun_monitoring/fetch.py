"""HTTP fetching and parsing of parkrun public data sources.

Two sources are used, both cheap and safe to poll a few times a week:

* ``events.json`` — the official catalogue of all active events worldwide
  (GeoJSON with coordinates), served from a CDN.
* ``globalChartNumRunnersAndEvents.php`` — an HTML page with a Google Chart
  whose embedded rows hold weekly totals (events / finishers / volunteers)
  since 2004, worldwide or per country. It still serves history for
  countries that left parkrun (e.g. Russia, France).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .config import COUNTRY_CHART_URL, EVENTS_JSON_URL

CHART_ROW_RE = re.compile(
    r'\[ new Date\("(\d{4}-\d{2}-\d{2})"\), ([\dNa.]+),([\dNa.]+),([\dNa.]+) \]'
)


@dataclass(frozen=True)
class CatalogueEvent:
    id: int
    eventname: str
    long_name: str | None
    short_name: str | None
    localised_long_name: str | None
    country_code: int
    series_id: int
    location: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class CatalogueCountry:
    code: int
    url: str | None
    bounds: tuple[float, float, float, float] | None  # west, south, east, north


@dataclass(frozen=True)
class WeeklyStat:
    week_date: str
    events: int | None
    finishers: int | None
    volunteers: int | None


def make_client(user_agent: str) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent},
        timeout=30.0,
        follow_redirects=True,
        transport=httpx.HTTPTransport(retries=2),
    )


def fetch_catalogue(
    client: httpx.Client,
) -> tuple[list[CatalogueCountry], list[CatalogueEvent]]:
    data = client.get(EVENTS_JSON_URL).raise_for_status().json()

    countries = []
    for code, info in data["countries"].items():
        bounds = info.get("bounds")
        countries.append(
            CatalogueCountry(
                code=int(code),
                url=info.get("url"),
                bounds=tuple(bounds) if bounds and len(bounds) == 4 else None,
            )
        )

    events = []
    for feature in data["events"]["features"]:
        props = feature["properties"]
        coords = (feature.get("geometry") or {}).get("coordinates") or (None, None)
        events.append(
            CatalogueEvent(
                id=int(feature["id"]),
                eventname=props["eventname"],
                long_name=props.get("EventLongName"),
                short_name=props.get("EventShortName"),
                localised_long_name=props.get("LocalisedEventLongName"),
                country_code=int(props["countrycode"]),
                series_id=int(props["seriesid"]),
                location=props.get("EventLocation"),
                latitude=coords[1],
                longitude=coords[0],
            )
        )
    return countries, events


def parse_chart(html: str) -> list[WeeklyStat]:
    def num(raw: str) -> int | None:
        return None if raw == "NaN" else int(float(raw))

    stats = []
    for date, events, finishers, volunteers in CHART_ROW_RE.findall(html):
        row = WeeklyStat(date, num(events), num(finishers), num(volunteers))
        # Trailing all-NaN rows (e.g. weeks after a country closed) carry no data.
        if (row.events, row.finishers, row.volunteers) != (None, None, None):
            stats.append(row)
    return stats


def fetch_country_stats(client: httpx.Client, country_code: int) -> list[WeeklyStat]:
    params = {} if country_code == 0 else {"CountryNum": country_code}
    response = client.get(COUNTRY_CHART_URL, params=params).raise_for_status()
    return parse_chart(response.text)
