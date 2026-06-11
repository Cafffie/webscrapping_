"""
Watford Palace Theatre scraper

Listing:  Two category-filtered pages (Drama/Play, Musical)
          requests + BeautifulSoup — event URLs extracted from anchor tags
Detail:   Event detail page — title, performance schedule (date/time/iid)
          and first price mention
Seat map: Spektrix ChooseSeats.aspx — server-rendered HTML with embedded JS
          seat data string. Parsed with regex; each performance is fetched
          independently by its own EventInstanceId.

Run:
    python watfordpalace_scraper.py
    python src/utils/csv_validator.py watfordpalace_output.csv
"""

from __future__ import annotations

import csv
import datetime
import json
import logging
import os as _os
import re
import sys as _sys
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# Make src.utils importable regardless of working directory
_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from src.utils.csv_validator import validate_csv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LISTING_URLS = [
    ("https://watfordpalacetheatre.co.uk/whats-on/?category=drama",   "Play"),
    ("https://watfordpalacetheatre.co.uk/whats-on/?category=musical", "Musical"),
]
BASE_URL      = "https://watfordpalacetheatre.co.uk"
SPEKTRIX_BASE = "https://tickets.watfordpalacetheatre.co.uk/watfordpalace/website"
RATE_LIMIT    = 1.5
OUTPUT_CSV    = "watfordpalace_output.csv"

CSV_COLUMNS = [
    "title", "venue_url", "category", "venue", "address", "city",
    "country", "open_date", "close_date", "booking_start_date",
    "booking_end_date", "upcoming_performances", "capacity", "currency",
    "is_limited_run", "seat_pricing", "scrape_datetime",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------
_ORDINAL_RE  = re.compile(r"(\d+)(st|nd|rd|th)", re.IGNORECASE)
_POSTCODE_RE = re.compile(r"[A-Z]{1,2}\d{1,2}[A-Z]?\s\d[A-Z]{2}", re.IGNORECASE)
_PRICE_RE    = re.compile(r"[£$€](\d+(?:\.\d{2})?)")
_AREA_RE     = re.compile(r'"areaNames"\s*:\s*(\{[^}]+\})')
_SEATDATA_RE = re.compile(r'"seatData"\s*:\s*"([^"]+)"')

# ---------------------------------------------------------------------------
# Venue info — populated at runtime from watfordpalacetheatre.co.uk
# ---------------------------------------------------------------------------
_venue: dict = {"name": "", "address": "", "city": "", "country": ""}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return sess


def fetch_soup(url: str, sess: requests.Session) -> BeautifulSoup:
    time.sleep(RATE_LIMIT)
    r = sess.get(url, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def clean(s: str) -> str:
    return " ".join(s.split())


def detect_currency(text: str) -> str:
    if "£" in text:
        return "GBP"
    if "€" in text:
        return "EUR"
    if "$" in text:
        return "USD"
    return ""


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------

def parse_event_date(s: str) -> Optional[str]:
    """'Thursday 30th April' → 'YYYY-MM-DD'; bumps year if the date is past."""
    s = _ORDINAL_RE.sub(r"\1", s)
    try:
        dt = dateparser.parse(s, dayfirst=True, fuzzy=True)
        if dt.date() < datetime.date.today():
            dt = dt.replace(year=dt.year + 1)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_event_time(s: str) -> Optional[str]:
    """'2:30 PM' or '19:30' → 'HH:MM' (24-hour)."""
    s = re.sub(r"\s+([ap]m)\b", r"\1", s.strip(), flags=re.IGNORECASE)
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.datetime.strptime(s.upper(), fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Phase 0 — Venue info extraction
# ---------------------------------------------------------------------------

def _extract_venue_from_soup(soup: BeautifulSoup) -> None:
    """Scan all <p> tags for one containing a UK postcode; parse venue fields."""
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if not _POSTCODE_RE.search(text):
            continue

        lines = [ln.strip() for ln in p.get_text("\n").splitlines() if ln.strip()]
        if len(lines) >= 2:
            name_part = lines[0].rstrip(",").strip()
            addr_line = lines[1].strip()
        else:
            idx = text.find(",")
            name_part = text[:idx].strip() if idx > 0 else ""
            addr_line = text[idx + 1:].strip() if idx > 0 else text

        pieces  = [s.strip() for s in addr_line.split(",")]
        address = pieces[0] if pieces else addr_line
        city    = pieces[-2] if len(pieces) >= 3 else (pieces[-1] if pieces else "")
        country = "United Kingdom" if _POSTCODE_RE.search(addr_line) else ""

        _venue.update({"name": name_part, "address": address,
                       "city": city, "country": country})
        log.info("Venue: %s | %s | %s | %s", name_part, address, city, country)
        return


def _load_venue_info(sess: requests.Session) -> None:
    """Populate _venue from the Watford Palace Theatre homepage."""
    soup = fetch_soup(BASE_URL + "/", sess)
    _extract_venue_from_soup(soup)
    if not _venue["name"]:
        log.warning("Venue info not found on homepage; will fall back to detail pages")


# ---------------------------------------------------------------------------
# Phase 1 — Event listing
# ---------------------------------------------------------------------------

def _fetch_event_list(sess: requests.Session) -> list[dict]:
    """Fetch both category pages and return all unique event dicts."""
    seen: set[str] = set()
    events: list[dict] = []

    for listing_url, category in LISTING_URLS:
        log.info("Fetching listing (%s): %s", category, listing_url)
        soup = fetch_soup(listing_url, sess)

        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if "/events/" not in href:
                continue
            url = href if href.startswith("http") else BASE_URL + href.rstrip("/") + "/"
            if url in seen:
                continue
            seen.add(url)

            h3 = a.find("h3")
            title = clean(h3.get_text() if h3 else a.get_text())
            if not title:
                continue

            events.append({"title": title, "url": url, "category": category})
            log.debug("  Found [%s]: %s", category, title)

    log.info("Listing complete: %d events", len(events))
    return events


# ---------------------------------------------------------------------------
# Phase 2 — Event detail page
# ---------------------------------------------------------------------------

def _fetch_event_detail(url: str, sess: requests.Session) -> dict:
    """Fetch event detail page; return title, performances, and first price."""
    soup = fetch_soup(url, sess)

    # Title
    h1 = soup.find("h1")
    title = clean(h1.get_text()) if h1 else ""
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].split(" - ")[0].strip()

    # Venue info fallback if homepage extraction failed
    if not _venue["name"]:
        _extract_venue_from_soup(soup)

    # Performances: .spektrix_booking--event blocks
    performances: list[dict] = []
    seen_keys: set[tuple] = set()

    for div in soup.find_all("div", class_="spektrix_booking--event"):
        date_el = div.find("div", class_="spektrix_booking--date")
        time_el = div.find("div", class_="spektrix_booking--time")
        link    = div.find("a", href=lambda h: h and "iid=" in h)
        if not (date_el and time_el and link):
            continue

        params = parse_qs(urlparse(link["href"]).query)
        iid = params.get("iid", [None])[0]
        if not iid:
            continue

        date_iso = parse_event_date(clean(date_el.get_text()))
        time_24  = parse_event_time(clean(time_el.get_text()))
        if date_iso and time_24 and (date_iso, time_24) not in seen_keys:
            seen_keys.add((date_iso, time_24))
            performances.append({"date_iso": date_iso, "time_24": time_24, "iid": iid})
            log.debug("    Perf: %s %s (iid=%s)", date_iso, time_24, iid)

    # First price mention on page
    pm = _PRICE_RE.search(soup.get_text())
    price_text = f"£{pm.group(1)}" if pm else ""

    return {"title": title, "performances": performances, "price_text": price_text}


# ---------------------------------------------------------------------------
# Phase 3 — Spektrix seat map
# ---------------------------------------------------------------------------

def _fetch_seat_map(iid: str, sess: requests.Session) -> tuple[list[dict], Optional[int]]:
    """
    Fetch ChooseSeats.aspx for one performance and parse available seats.

    Seat data is embedded in the HTML as a JS string:
      "seatData": "id|areaId|x|y|...|label||avail|...|nextId;..."
    field[11] = label (seat name + price), field[1] = areaId,
    field[13] = "1" when available.

    Area names come from:
      "areaNames": {"2816": "Main Auditorium", ...}

    Returns (seats, total_capacity). Capacity = total seat entries parsed.
    """
    url = (
        f"{SPEKTRIX_BASE}/ChooseSeats.aspx"
        f"?EventInstanceId={iid}&ChooseAttendee=false&resize=true"
    )
    try:
        soup = fetch_soup(url, sess)
        html = str(soup)
    except Exception as exc:
        log.warning("Seat map fetch failed (iid=%s): %s", iid, exc)
        return [], None

    # Area names
    area_names: dict[str, str] = {}
    m = _AREA_RE.search(html)
    if m:
        try:
            area_names = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # Seat data string
    m = _SEATDATA_RE.search(html)
    if not m:
        log.info("  No seat data for iid=%s", iid)
        return [], None

    seats: list[dict] = []
    total = 0

    for entry in m.group(1).split(";"):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split("|")
        if len(fields) < 12:
            continue
        total += 1

        available = len(fields) > 13 and fields[13] == "1"
        if not available:
            continue

        label     = fields[11]
        area_id   = fields[1]
        area_name = area_names.get(area_id, "")

        pm = _PRICE_RE.search(label)
        if not pm:
            continue
        ticket_price = float(pm.group(1))

        nm = re.match(r"^(.+?)\s*-\s*[£$€]", label)
        seat_name = nm.group(1).strip() if nm else label
        seat_str  = f"{seat_name} - {area_name}" if area_name else seat_name

        seats.append({"seat": seat_str, "ticket_price": ticket_price})

    log.info("  Seat map: %d available / %d total (iid=%s)", len(seats), total, iid)
    return seats, total if total > 0 else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sess = make_session()
    _load_venue_info(sess)
    events = _fetch_event_list(sess)

    rows: list[dict] = []
    for ev in events:
        log.info("Scraping: %s", ev["url"])
        try:
            detail = _fetch_event_detail(ev["url"], sess)
        except Exception as exc:
            log.warning("Detail fetch failed (%s): %s", ev["url"], exc)
            continue

        perfs = detail["performances"]
        if not perfs:
            log.warning("No performances found for: %s", detail["title"])
            continue

        seat_pricing: dict[str, list] = {}
        capacity = 0

        for perf in perfs:
            seats, cap = _fetch_seat_map(perf["iid"], sess)
            if cap is not None:
                capacity = max(capacity, cap)
            if seats:
                key = f"{perf['date_iso']} {perf['time_24']}"
                seat_pricing[key] = seats

        open_date  = perfs[0]["date_iso"]
        close_date = perfs[-1]["date_iso"]
        upcoming   = [{"date": p["date_iso"], "time": p["time_24"]} for p in perfs]
        currency   = detect_currency(detail["price_text"]) or ("GBP" if seat_pricing else "")

        rows.append({
            "title":               detail["title"],
            "venue_url":           ev["url"],
            "category":            ev["category"],
            "venue":               _venue["name"],
            "address":             _venue["address"],
            "city":                _venue["city"],
            "country":             _venue["country"],
            "open_date":           open_date,
            "close_date":          close_date,
            "booking_start_date":  open_date,
            "booking_end_date":    close_date,
            "upcoming_performances": repr(upcoming),
            "capacity":            capacity if capacity else "",
            "currency":            currency,
            "is_limited_run":      "True" if close_date else "False",
            "seat_pricing":        repr(seat_pricing),
            "scrape_datetime":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        log.info(
            "OK: %-45s perfs=%-2d cap=%-5s pricing_keys=%d",
            detail["title"], len(perfs), capacity, len(seat_pricing),
        )

    if not rows:
        log.error("No data scraped!")
        return

    # Backfill capacity with venue-wide max for rows that had no seat layout yet
    venue_cap = max((int(r["capacity"]) for r in rows if r.get("capacity")), default=0)
    if venue_cap:
        for r in rows:
            if not r.get("capacity"):
                r["capacity"] = str(venue_cap)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    log.info("Wrote %d rows -> %s", len(rows), OUTPUT_CSV)

    log.info("Running validator: %s", OUTPUT_CSV)
    report = validate_csv(OUTPUT_CSV)
    if not report.passed:
        log.error("Validation FAILED")
    else:
        log.info("Validation PASSED")


if __name__ == "__main__":
    main()
