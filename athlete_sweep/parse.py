"""Парсер страницы атлета parkrun (/parkrunner/{id}/all/).

Логика перенесена один-в-один из боевого парсера saturday_runs
(app/parkrun/parsers/athlete.py), но без app-зависимостей — только bs4+re,
возвращает простые структуры. /all содержит всё нужное (имя, штрихкод, забеги,
волонтёрство), поэтому фетчим одну страницу на атлета.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup

NAME_BARCODE_RE = re.compile(r"^(.+?)\s*\(A(\d+)\)\s*$", re.IGNORECASE)
AGE_CATEGORY_RE = re.compile(r"most recent age category was\s+([A-Za-z0-9\-]+)", re.IGNORECASE)
TOTAL_RUNS_RE = re.compile(r"(\d+)\s+parkruns?\s+total", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


@dataclass
class AthleteData:
    status: str                        # ok | not_found | registered_empty | unclassified
    name: str | None = None
    barcode: str | None = None
    age_category: str | None = None
    total_runs: int | None = None
    runs: list[dict] = field(default_factory=list)
    volunteer_detail: list[dict] = field(default_factory=list)  # [{role, occasions}]
    volunteer_total: int = 0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ").strip().lower())


def _is_total_credits(label: str) -> bool:
    return "total" in (label or "").lower()


def _find_table(soup: BeautifulSoup, heading_substring: str):
    needle = re.sub(r"\s+", " ", heading_substring.lower())
    for heading in soup.find_all(["h2", "h3", "h4", "caption"]):
        if needle not in re.sub(r"\s+", " ", heading.get_text(" ", strip=True).lower()):
            continue
        if heading.name == "caption" and heading.parent is not None and heading.parent.name == "table":
            return heading.parent
        table = heading.find_next("table")
        if table is not None:
            return table
        parent_table = heading.find_parent("table")
        if parent_table is not None:
            return parent_table
    return None


def _headers(table) -> list[str]:
    for row in table.find_all("tr"):
        cells = row.find_all("th")
        if cells:
            return [_norm(c.get_text()) for c in cells]
    return []


def _cells(row) -> list[str]:
    return [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]


def _by_header(headers: list[str], cells: list[str], *names: str) -> str | None:
    for name in names:
        key = _norm(name)
        if key in headers:
            idx = headers.index(key)
            if idx < len(cells):
                return cells[idx]
    return None


def _name_barcode(soup) -> tuple[str, str] | None:
    for heading in soup.find_all("h2"):
        m = NAME_BARCODE_RE.match(heading.get_text(" ", strip=True))
        if m:
            return m.group(1).strip(), m.group(2)
    return None


def _volunteer(soup) -> tuple[list[dict], int]:
    table = _find_table(soup, "volunteer summary")
    if table is None:
        return [], 0
    detail: list[dict] = []
    occ_sum = 0
    total_credits: int | None = None
    headers = _headers(table)
    body = table.find("tbody")
    rows = body.find_all("tr") if body is not None else table.find_all("tr")
    for row in rows:
        if row.find_parent("tfoot") is not None:
            continue
        cells = _cells(row)
        if not cells or cells == headers or (row.find("th") and not row.find("td")):
            continue
        role = (_by_header(headers, cells, "role") or "").strip()
        occ_raw = _by_header(headers, cells, "occasions")
        if not role or role.lower() == "role" or _is_total_credits(role):
            continue
        occ = int(occ_raw) if occ_raw and occ_raw.isdigit() else 0
        occ_sum += occ
        detail.append({"role": role, "occasions": occ})
    tfoot = table.find("tfoot")
    if tfoot is not None:
        for row in tfoot.find_all("tr"):
            cells = _cells(row)
            if len(cells) >= 2 and _is_total_credits(cells[0]):
                d = re.search(r"\d+", cells[1])
                if d:
                    total_credits = int(d.group())
    return detail, total_credits if total_credits is not None else occ_sum


def _parse_runs(soup, athlete_id: str) -> list[dict]:
    table = _find_table(soup, "all results")
    if table is None:
        return []
    headers = _headers(table)
    out: list[dict] = []
    for row in table.find_all("tr"):
        cells = _cells(row)
        if not cells or cells == headers:
            continue
        event = _by_header(headers, cells, "event")
        date_raw = _by_header(headers, cells, "run date")
        if not event or not date_raw or not DATE_RE.match(date_raw):
            continue
        num_raw = _by_header(headers, cells, "run number")
        pos_raw = _by_header(headers, cells, "pos", "position")
        time_raw = _by_header(headers, cells, "time")
        age_grade = _by_header(headers, cells, "age grade", "agegrade")
        out.append({
            "event_slug": re.sub(r"[^a-z0-9]+", "-", event.lower().strip()).strip("-") or "unknown",
            "event_name": event,
            "run_date": datetime.strptime(date_raw, "%d/%m/%Y").date(),
            "run_number": int(num_raw) if num_raw and num_raw.isdigit() else None,
            "position": int(pos_raw) if pos_raw and pos_raw.isdigit() else None,
            "finish_time_sec": _time_sec(time_raw or ""),
            "age_grade": age_grade,
        })
    return out


def _time_sec(v: str) -> int | None:
    v = v.strip()
    if not v or v in {"-", "—"}:
        return None
    parts = v.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None


def parse_summary(html: str, athlete_id: str) -> AthleteData:
    """Разобрать SUMMARY-страницу атлета (/parkrunner/{id}/): имя, штрихкод, age,
    всего пробежек и ВОЛОНТЁРСТВО (оно только здесь, не на /all). Забеги —
    отдельно из /all (parse_all_runs). Классификация: not_found / ok /
    registered_empty / unclassified (последнее — сырой HTML на ревью)."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True).lower() if soup.title else ""
    head200 = soup.get_text(" ", strip=True).lower()[:200]
    if "page not found" in title or "403 forbidden" in head200:
        return AthleteData(status="not_found")

    nb = _name_barcode(soup)
    if nb is None:
        # Нет имени+штрихкода. Пустая зарегистрированная (как /1) — короткая,
        # без структуры атлета; всё прочее — на ревью.
        looks_athlete = "parkruns total" in html.lower() or "volunteer summary" in html.lower()
        if len(html) < 20000 and not looks_athlete:
            return AthleteData(status="registered_empty")
        return AthleteData(status="unclassified")

    name, barcode = nb
    if barcode != athlete_id:
        return AthleteData(status="unclassified")  # id на странице не совпал — на ревью

    page_text = soup.get_text(" ", strip=True)
    total_runs = int(m.group(1)) if (m := TOTAL_RUNS_RE.search(page_text)) else None
    age = a.group(1) if (a := AGE_CATEGORY_RE.search(page_text)) else None
    vol_detail, vol_total = _volunteer(soup)
    return AthleteData(
        status="ok", name=name, barcode=f"A{barcode}", age_category=age,
        total_runs=total_runs, volunteer_detail=vol_detail, volunteer_total=vol_total,
    )


def parse_all_runs(html: str, athlete_id: str) -> list[dict]:
    """Разобрать /all-страницу: полная история забегов (таблица All Results)."""
    return _parse_runs(BeautifulSoup(html, "html.parser"), athlete_id)
