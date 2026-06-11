"""Reusable helper functions for web scraping."""
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import country_converter as coco
import dateparser
import pycountry
import requests
from babel.numbers import get_territory_currencies
from bs4 import BeautifulSoup
from dateutil import parser, tz
from deep_translator import GoogleTranslator
from geopy.geocoders import Nominatim
from price_parser import Price
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils.config import Config

config = Config()

# Shared month-name → integer mapping.
# Covers full names (lowercase), 3-letter abbreviations (lower and title case),
# and common variants (Sept/sept) so scrapers don't need their own copies.
MONTH_MAP = {
    # Full lowercase names
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    # 3-letter lowercase
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    # 3-letter title case
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
    # Extra variants
    "Sept": 9,
    "sept": 9,
}

# Initialize the logger
logger = logging.getLogger(__name__)


class Translator:
    """A wrapper for GoogleTranslator for consistent use across scrapers."""

    def __init__(self, source: str = "auto", target: str = "en"):
        self.translator = GoogleTranslator(source=source, target=target)

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        try:
            return self.translator.translate(text)
        except Exception as e:
            logger.warning(f"Translation failed: {e}")
            return text


def safe_maximize(driver, width=1920, height=1080):
    """Safely maximize browser window."""
    try:
        driver.set_window_size(width, height)
    except Exception as e:
        logger.debug("Failed to set window size: %s", e)


def safe_get(driver, url, wait_element_id="content", wait=20, logger=logger):
    """Safely load a URL and wait for element."""
    try:
        if logger:
            logger.info(f"Loading URL: {url}")
        driver.get(url)
        WebDriverWait(driver, wait).until(
            EC.presence_of_element_located((By.ID, wait_element_id))
        )
        if logger:
            logger.info(f"Page loaded and ready: {url}")
        return True
    except Exception as e:
        if logger:
            logger.error(f"Failed to load page: {url} | {e}")
        return None


def safe_get_text(driver, xpath, timeout=10, logger=logger):
    """Safely get text from element by XPath."""
    try:
        element = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
        return element.text.strip()
    except Exception:
        if logger:
            logger.warning(f"Element not found for XPath: {xpath}")
        return None


def safe_find_child(element, xpath, many=False):
    """Safely find child element(s)."""
    try:
        return (
            element.find_elements(By.XPATH, xpath)
            if many
            else element.find_element(By.XPATH, xpath)
        )
    except Exception as e:
        logger.debug("Child element not found for XPath: %s | %s", xpath, e)
        return [] if many else None


def safe_click(driver, xpath):
    """Safely click element by XPath."""
    try:
        element = driver.find_element(By.XPATH, xpath)
        element.click()
        return True
    except Exception as e:
        logger.warning("Failed to click element at XPath: %s | %s", xpath, e)
        return False


def safe_current_url(driver):
    """Safely get current URL."""
    try:
        return driver.current_url
    except Exception as e:
        logger.warning("Failed to get current URL: %s", e)
        return None


def click_next(driver, xpath):
    """Click next button if not disabled."""
    try:
        btn = driver.find_element(By.XPATH, xpath)
    except NoSuchElementException:
        logger.debug("Next button not found at XPath: %s", xpath)
        return False

    try:
        cls = btn.get_attribute("class") or ""
    except Exception:
        cls = ""

    if "disabled" in cls.lower():
        logger.debug("Next button is disabled, stopping pagination.")
        return False

    try:
        driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        logger.warning("Failed to click next button: %s", e)
        return False

    return True


def accept_cookies(
    driver,
    xpath: str = "//*[@id='onetrust-accept-btn-handler']",
    *,
    timeout: int = 3,
    logger=logger,
    once_per_domain: bool = True,
) -> bool:
    """Accept cookie banner if present.

    - waits briefly for the element to become clickable
    - swallows "not clickable" style errors, with a JS-click fallback
    - avoids repeatedly trying on every page: once per domain per driver session
    """
    domain = None
    try:
        current_url = getattr(driver, "current_url", None) or ""
        domain = urlparse(current_url).netloc or None
    except Exception:
        domain = None

    if once_per_domain and domain:
        attempted = getattr(driver, "_ovation_cookie_attempted_domains", None)
        if attempted is None:
            attempted = set()
            setattr(driver, "_ovation_cookie_attempted_domains", attempted)
        if domain in attempted:
            return False
        attempted.add(domain)

    try:
        element = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
    except TimeoutException:
        return False
    except Exception:
        return False

    try:
        element.click()
        if logger:
            logger.info("Cookies accepted.")
        return True
    except (
        ElementClickInterceptedException,
        ElementNotInteractableException,
        StaleElementReferenceException,
        WebDriverException,
    ):
        try:
            driver.execute_script("arguments[0].click();", element)
            if logger:
                logger.info("Cookies accepted (js-click).")
            return True
        except Exception as e:
            if logger:
                logger.warning("Failed to accept cookies via JS click: %s", e)
            return False


def extract_postcode(address, region="US"):
    """Extract postcode from address (US or UK)."""
    try:
        if not address or not isinstance(address, str):
            return None
        if region == "UK":
            m = re.search(r"[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2}", address)
        else:
            m = re.search(r"\b\d{5}(?:-\d{4})?\b", address)
        return m.group(0) if m else None
    except Exception:
        return None


def extract_postcode_US(address):
    try:
        if not address or not isinstance(address, str):
            return None

        m = re.search(r"\b\d{5}(?:-\d{4})?\b", address)
        return m.group(0) if m else None
    except Exception:
        return None


def extract_city(address, region="US"):
    """Extract city from address."""
    if not address:
        return None

    pc = extract_postcode(address, region)
    if not pc:
        return None

    before = address.replace(pc, "").strip()
    parts = [p.strip() for p in before.split(",") if p.strip()]

    if not parts:
        return None

    if region == "UK":
        country_terms = {"uk", "united kingdom", "england", "scotland", "wales"}
    else:
        country_terms = {"us", "united states of america", "united states", "usa"}

    if parts[-1].lower() in country_terms:
        parts = parts[:-1]

    return parts[-1] if parts else None


def resolve_country_code(country: str) -> Optional[str]:
    """
    Convert full country name or ISO code into ISO alpha-2 code.
    """
    if not country:
        return None

    country = country.strip()

    # If already ISO code
    if len(country) == 2:
        return country.upper()

    try:
        result = pycountry.countries.search_fuzzy(country)[0]
        return result.alpha_2
    except LookupError:
        logger.warning("Could not resolve country code for: %s", country)
        return None


_SYMBOL_TO_ISO = {
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₩": "KRW",
    "₹": "INR",
    "₽": "RUB",
    "฿": "THB",
    "₺": "TRY",
    "$": "USD",
}


def get_currency_from_price(
    price_string: str,
    country: Optional[str] = None,
    default_currency: Optional[str] = None,
) -> Optional[str]:
    """
    Extract and normalize currency from a price string.

    Resolution order:
    1. Already a 3-letter ISO code → return as-is.
    2. Symbol + country provided → resolve via territory lookup (handles ambiguous symbols like $).
    3. Symbol found in known symbol map → return ISO code.
    4. default_currency fallback.

    Args:
        price_string (str): String containing price with currency symbol or ISO code.
        country (str, optional): Full country name or ISO code to resolve ambiguous symbols.
        default_currency (str, optional): Fallback currency code.

    Returns:
        str | None: ISO currency code.
    """
    price = Price.fromstring(price_string)

    if price.currency:
        currency = price.currency.strip().upper()

        # 1. Already ISO code
        if len(currency) == 3 and currency.isalpha():
            return currency

        # 2. Country-based resolution (most accurate for ambiguous symbols)
        if country:
            country_code = resolve_country_code(country)
            if country_code:
                try:
                    currencies = get_territory_currencies(country_code)
                    return currencies[0] if currencies else None
                except Exception as e:
                    logger.warning(
                        "Territory currency lookup failed for %s: %s", country_code, e
                    )

        # 3. Symbol map fallback
        iso = _SYMBOL_TO_ISO.get(price.currency.strip())
        if iso:
            return iso

    return default_currency or None


def parse_date_string_to_datetime(date_string, date_format="%Y-%m-%d"):
    """Convert date string to datetime object.

    Args:
        date_string: Date string to parse
        date_format: Format string (default: "%Y-%m-%d")

    Returns:
        datetime: Parsed datetime object or None

    Examples:
        >>> parse_date_string_to_datetime("2024-12-20")
        datetime.datetime(2024, 12, 20, 0, 0)
        >>> parse_date_string_to_datetime("2024-12-20 14:30:00", "%Y-%m-%d %H:%M:%S")
        datetime.datetime(2024, 12, 20, 14, 30)
        >>> parse_date_string_to_datetime(None)
        None
    """
    if not date_string:
        return None
    try:
        return datetime.strptime(date_string, date_format)
    except Exception as e:
        logger.warning(
            "Failed to parse date string '%s' with format '%s': %s",
            date_string,
            date_format,
            e,
        )
        return None


def normalize_country(raw_country):
    """Normalize country name using coco + pycountry fallback."""
    if not raw_country or not raw_country.strip():
        return None

    raw_country = raw_country.strip().lower()
    # Try coco first
    try:
        std = coco.convert(names=raw_country, to="name_short", not_found=None)
        if std:
            return std
    except Exception:
        logger.warning("Coco failed, fallback to pycountry")
        pass

    # Fallback to pycountry
    try:
        country = pycountry.countries.lookup(raw_country)
        return country.name
    except LookupError:
        return raw_country


def parse_date_range(date_text):
    """Parse date text and return open_date and close_date in ISO format.

    Handles formats like:
    - "20/12/2024"
    - "5 - 10 December 2024"
    - "20 December 2024"

    Args:
        date_text: Date string to parse

    Returns:
        tuple: (open_date, close_date) in YYYY-MM-DD format or None

    Examples:
        >>> parse_date_range("20/12/2024")
        ('2024-12-20', None)
        >>> parse_date_range("5 - 10 December 2024")
        ('2024-12-05', '2024-12-10')
        >>> parse_date_range("20 December 2024")
        ('2024-12-20', None)
        >>> parse_date_range(None)
        (None, None)
    """
    if not date_text:
        return None, None

    # Remove time components
    date_text = re.sub(r"\s+\d{1,2}:\d{2}", "", date_text).strip()

    # Check if already in DD/MM/YYYY format
    if re.match(r"\d{2}/\d{2}/\d{4}", date_text):
        try:
            parsed = datetime.strptime(date_text, "%d/%m/%Y")
            return format_date_to_iso(parsed), None
        except Exception as e:
            logger.warning("Failed to parse DD/MM/YYYY date '%s': %s", date_text, e)
            return None, None

    # Check for date range: "5 - 10 December 2024"
    range_match = re.match(r"(\d{1,2})\s*-\s*(\d{1,2})\s+(\w+)\s+(\d{4})", date_text)
    if range_match:
        start_day, end_day, month, year = range_match.groups()
        try:
            start_date = datetime.strptime(f"{start_day} {month} {year}", "%d %B %Y")
            end_date = datetime.strptime(f"{end_day} {month} {year}", "%d %B %Y")
            return format_date_to_iso(start_date), format_date_to_iso(end_date)
        except Exception as e:
            logger.warning("Failed to parse date range '%s': %s", date_text, e)
            return None, None

    # Try parsing single date
    try:
        parsed_date = parser.parse(date_text, dayfirst=None)
        return format_date_to_iso(parsed_date), None
    except Exception as e:
        logger.warning("Failed to parse date '%s': %s", date_text, e)
        return None, None


def parse_date_time_from_text(date_text):
    """Parse date and time from different text formats and return time in 24-hour format."""
    if not date_text:
        return None, None

    # Extract time
    time_match = re.search(r"\d{1,2}:\d{2}(?:\s?[AP]M)?", date_text, re.I)
    time_val = None

    if time_match:
        raw_time = time_match.group(0).strip()

        try:
            if "AM" in raw_time.upper() or "PM" in raw_time.upper():
                time_obj = datetime.strptime(raw_time.upper(), "%I:%M %p")
            else:
                time_obj = datetime.strptime(raw_time, "%H:%M")

            time_val = time_obj.strftime("%H:%M")
        except ValueError as e:
            logger.warning("Failed to parse time '%s': %s", raw_time, e)
            time_val = None

    # Remove weekday and bullet
    cleaned = re.sub(r"^[A-Za-z]+,\s*", "", date_text)
    cleaned = cleaned.split("•")[0].strip()

    date_formats = [
        "%d %B %Y",  # 14 March 2026
        "%B %d, %Y",  # July 31, 2026
    ]

    date_obj = None
    for fmt in date_formats:
        try:
            date_obj = datetime.strptime(cleaned, fmt)
            break
        except ValueError:
            continue

    date = date_obj.strftime("%Y-%m-%d") if date_obj else None

    return date, time_val


def clean_price(ticket_price):
    """Clean price string to numeric value."""
    if not ticket_price:
        return None
    ticket_price = re.sub(r"[^\d.,]", "", ticket_price)
    ticket_price = ticket_price.replace(",", "")
    if not ticket_price:
        return 0.0
    return float(ticket_price)


def get_raw_people_text(driver):
    """Extract raw people text from cast section."""
    try:
        els = driver.find_elements(
            By.XPATH,
            "//div[contains(@class, 'Cast__ActorDescription-sc-1j2rq93-5')]//div",
        )
        texts = []
        for e in els:
            txt = driver.execute_script("return arguments[0].textContent;", e)
            if txt:
                texts.append(txt)
        return texts
    except Exception as e:
        logger.warning("Failed to extract cast text: %s", e)
        return []


def standardize_category(category_str: str) -> str:
    """
    Standardize category types to singular form.

    Priority order:
        1. Musical — matched by 'musical', 'musicals', 'music*', or 'concert'
        2. Play    — matched by 'play' or 'plays'
        3. Drama   — fallback for 'drama', 'teatro', or 'theatre' → returns "Play"

    Args:
        category_str (str): The raw category string to be standardized.
                            May contain multiple comma-separated categories
                            (e.g. "Broadway, Drama, Musical").

    Returns:
        str | None: A standardized category label, or None if no match is found.

    Examples:
        >>> standardize_category("Broadway, Drama, Musical")
        'Musical'
        >>> standardize_category("Broadway, Drama, Play")
        'Play'
        >>> standardize_category("Drama Playhouse")
        'Play'
        >>> standardize_category("Comedy Night")
        None
    """
    if not category_str or not isinstance(category_str, str):
        return None

    text = category_str.lower().strip()

    has_musical = bool(re.search(r"\bmusical", text))
    has_play = bool(re.search(r"\bplay\b", text))
    has_drama = bool("drama" in text or "teatro" in text or "theatre" in text)

    # 1. Musical takes highest priority
    if has_musical:
        return "Musical"

    # 2. Explicit "play" takes next priority
    if has_play:
        return "Play"

    # 3. Drama is a sub-category signal — maps to Play
    if has_drama:
        return "Play"

    return None


def _do_search(sb, venue_name):
    search_query = f"{venue_name} capacity"
    logger.debug("Searching Google for venue capacity: %s", venue_name)
    sb.open(f"https://www.google.com/search?q={search_query}")
    sb.sleep(2)

    soup = sb.get_beautiful_soup()

    # Try to find capacity in featured snippet or knowledge panel
    capacity_patterns = [
        r"capacity[:\s]+([\d,]+)",
        r"([\d,]+)\s*capacity",
        r"seats[:\s]+([\d,]+)",
        r"([\d,]+)\s*seats",
    ]

    text = soup.get_text()
    for pattern in capacity_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            capacity = match.group(1).replace(",", "")
            logger.debug("Found capacity %s for venue: %s", capacity, venue_name)
            return int(capacity)
    logger.debug("No capacity found for venue: %s", venue_name)
    return None


def scrape_google_venue_capacity(venue_name, sb_instance=None):
    """Google search venue capacity using an existing or new SeleniumBase instance."""
    if not venue_name:
        return None

    try:
        if sb_instance:
            return _do_search(sb_instance)
        else:
            from seleniumbase import SB

            with SB(uc=True, headless=True) as sb:
                return _do_search(sb, venue_name)
    except Exception as e:
        logger.error("Error getting capacity for %s: %s", venue_name, e)
        return None


def human_delay(min_s=1.5, max_s=4.0):
    """Random pause to mimic human reading/thinking time with logging."""
    delay = random.uniform(min_s, max_s)
    logger.debug(f"Human delay: sleeping for {delay:.2f} seconds...")
    time.sleep(delay)


def safe_current_url_denver(sb):
    """Safely get the current URL from the SB object."""
    try:
        return sb.get_current_url()
    except Exception as e:
        logger.warning("Failed to get current URL: %s", e)
        return None


def safe_get_denver(sb, url, wait=10):
    """
    Refactored to use UC (Undetected) mode.
    Handles the Reese84 handshake automatically.
    """
    try:
        logger.info("Loading URL: %s", url)
        # uc_open_with_reconnect bypasses the initial Imperva/Reese84 block
        sb.uc_open_with_reconnect(url, reconnect_time=wait if wait > 4 else 4)

        # Handle the Reese84 'Pardon our interruption' checkbox if it appears
        if (
            "captcha" in sb.get_current_url().lower()
            or "distil" in sb.get_page_source().lower()
        ):
            logger.warning("Reese84 detected. Executing human-like solve...")
            sb.uc_gui_handle_captcha()
            time.sleep(random.uniform(2, 4))

        logger.info("Page loaded successfully: %s", url)
        return True

    except Exception as e:
        logger.error("Failed to load page: %s | Exception: %s", url, repr(e))
        return None


def safe_get_text_denver(sb, xpath, timeout=5):
    """Uses SB's built-in wait logic which is more resistant to stale elements."""
    try:
        # wait_for_element_visible is better for behavioral sensors
        sb.wait_for_element_visible(xpath, timeout=timeout)
        return sb.get_text(xpath).strip()
    except Exception:
        logger.warning("Element not found for XPath: %s", xpath)
        return None


def accept_cookies_denver(sb):
    """Handles the cookie banner with a human-like delay."""
    cookie_xpath = "//*[@id='onetrust-accept-btn-handler']"
    try:
        if sb.is_element_visible(cookie_xpath):
            time.sleep(random.uniform(1, 2.5))  # Human hesitation
            sb.click(cookie_xpath)
            logger.info("Cookies accepted.")
    except Exception:
        pass


def parse_datetime(datetime_raw):
    if not datetime_raw:
        return None, None

    time_match = re.search(r"\b\d{1,2}:\d{2}\s?(AM|PM)\b", datetime_raw, re.IGNORECASE)
    time_str = time_match.group(0) if time_match else None

    date_clean = re.sub(
        r"\b\d{1,2}:\d{2}\s?(AM|PM)\b", "", datetime_raw, flags=re.IGNORECASE
    )
    date_clean = re.sub(r"[\n\r,]+", "", date_clean).strip()
    date_clean = re.sub(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+",
        "",
        date_clean,
        flags=re.IGNORECASE,
    )
    try:
        date_parsed = datetime.strptime(date_clean, "%B %d %Y").date()
    except Exception as e:
        logger.warning("Failed to parse datetime '%s': %s", date_clean, e)
        date_parsed = None

    return date_parsed, time_str


def safe_execute_text(sb, script):
    try:
        result = sb.execute_script(script)
        return result.strip() if isinstance(result, str) else result
    except Exception as e:
        logger.warning("Script execution failed: %s", e)
        return None


def get_ticket_url(sb):
    try:
        tag = sb.find_element("//a[@aria-label='GET TICKETS']")
        return tag.get_attribute("href")
    except Exception as e:
        logger.debug("GET TICKETS link not found: %s", e)
        return None


def venue_to_slug(venue):
    if not venue or not isinstance(venue, str):
        return None
    venue = venue.strip().lower()
    venue = re.sub(r"\s+", "-", venue)
    venue = re.sub(r"[^a-z0-9\-]", "", venue)
    return venue


def get_city_from_postcode_uk(postcode):
    """Return city/admin_district from a UK postcode using postcodes.io."""
    if not postcode:
        return None

    # Remove spaces for API call
    postcode_clean = postcode.replace(" ", "")
    url = f"https://api.postcodes.io/postcodes/{postcode_clean}"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            logger.warning(
                "postcodes.io returned %s for postcode: %s", r.status_code, postcode
            )
            return None
        data = r.json()
        return data.get("result", {}).get("admin_district")  # city/town
    except requests.RequestException as e:
        logger.warning("Postcode lookup failed for %s: %s", postcode, e)
        return None


def extract_city_uk(address):
    """Extract city from a UK address using postcode lookup."""
    if not address:
        return None

    # Step 1: Extract postcode using your existing helper
    postcode = extract_postcode(address, region="UK")
    if not postcode:
        return None

    # Step 2: Lookup city from postcode
    city = get_city_from_postcode_uk(postcode)
    return city


def format_date_string(day_str, month_str, header_year_raw):
    year_match = re.search(r"(\d{4})", header_year_raw)
    year = year_match.group(1) if year_match else None

    date_str = f"{day_str} {month_str} {year}"
    dt = dateparser.parse(date_str, languages=["en", "hu"])  # English + Hungarian
    if not dt:
        logger.warning("Could not parse date string: %s", date_str)
    return dt.strftime("%Y-%m-%d") if dt else ""


# Map common timezone abbreviations to tz database names
TZINFOS = {
    "CT": tz.gettz("America/Chicago"),
    "ET": tz.gettz("America/New_York"),
    "PT": tz.gettz("America/Los_Angeles"),
    "MT": tz.gettz("America/Denver"),
    "UTC": tz.gettz("UTC"),
}


def convert_to_24hr(time_str, timezone=None):
    """Convert time to 24-hour format, optionally attaching timezone info.

    Example runs:
    print(convert_to_24hr("5 pm CT"))        # "17:00 CST"
    print(convert_to_24hr("8:30 am ET"))     # "08:30 EST"
    print(convert_to_24hr("14:45 UTC"))      # "14:45 UTC"
    print(convert_to_24hr("7 pm"))           # "19:00"
    print(convert_to_24hr("14:30"))          # "14:30"
    print(convert_to_24hr("14:30", "PT"))    # "14:30 PST"
    """
    try:
        if not time_str:
            return None

        time_str = time_str.strip()
        time_str = re.sub(r"([ap])\.m\.?", r"\1m", time_str, flags=re.IGNORECASE)

        # If timezone not provided, try to extract abbreviation
        tzinfo = None
        if timezone:
            tzinfo = TZINFOS.get(timezone.upper())
        else:
            tz_match = re.search(r"\b([A-Z]{2,4})\b", time_str.upper())
            if tz_match and tz_match.group(1) not in ("AM", "PM"):
                tz_abbr = tz_match.group(1)
                tzinfo = TZINFOS.get(tz_abbr)
                # Remove the abbreviation from the string for parsing
                time_str = re.sub(
                    r"\b" + tz_abbr + r"\b", "", time_str, flags=re.IGNORECASE
                ).strip()

        # Normalize AM/PM spacing (turn "7:00pm" / "7:00 pm" into "7:00 pm")
        time_str = re.sub(r"\s*([ap]m)$", r" \1", time_str.lower())

        # Detect if already in 24-hour format (e.g., "14:30")
        if re.match(r"^\d{1,2}:\d{2}$", time_str):
            dt = datetime.strptime(time_str, "%H:%M")
        else:
            # Normalize AM/PM format
            clean_time = re.sub(r"[^0-9: apm]", "", time_str.lower().replace(".", ":"))
            if "am" in clean_time or "pm" in clean_time:
                if ":" not in clean_time:
                    clean_time = clean_time.replace("am", ":00 am").replace(
                        "pm", ":00 pm"
                    )
                dt = datetime.strptime(clean_time, "%I:%M %p")
            else:
                dt = datetime.strptime(clean_time, "%H:%M")

        # Attach timezone if available
        if tzinfo:
            dt = dt.replace(tzinfo=tzinfo)
        return dt.strftime("%H:%M %Z") if tzinfo else dt.strftime("%H:%M")
    except Exception:
        logger.exception("Time conversion error for: %s", time_str)
        return None


def format_date_to_iso(date_input):
    """Format date to ISO format (YYYY-MM-DD).

    Args:
        date_input: Date string, datetime object, or None

    Returns:
        str: Date in YYYY-MM-DD format or None

    Examples:
        >>> format_date_to_iso("2024-12-20")
        '2024-12-20'
        >>> format_date_to_iso("20/12/2024")
        '2024-12-20'
        >>> format_date_to_iso("December 20, 2024")
        '2024-12-20'
        >>> from datetime import datetime
        >>> format_date_to_iso(datetime(2024, 12, 20))
        '2024-12-20'
        >>> format_date_to_iso(None)
        None
    """
    if not date_input:
        return None

    # If already a string, check if it's in ISO format
    if isinstance(date_input, str):
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_input):
            return date_input
        try:
            parsed = parser.parse(date_input, dayfirst=None)
            return parsed.strftime("%Y-%m-%d")
        except Exception as e:
            logger.warning("Failed to parse date string '%s': %s", date_input, e)
            return None

    # If datetime object
    try:
        return date_input.strftime("%Y-%m-%d")
    except Exception as e:
        logger.warning("Failed to format date object '%s': %s", date_input, e)
        return None


def safe_translate(translator, text, fallback=None):
    """Safely translate text with fallback."""
    try:
        return translator.translate(text) if text else fallback
    except Exception as e:
        logger.warning("Translation failed for text '%s': %s", text, e)
        return fallback or text


def parse_booking_dates(text):
    if not text:
        return {"start_date": None, "end_date": None}

    text = text.replace("–", "-").replace("—", "-").replace("−", "-").strip()
    # Strip ordinal suffixes: 1st, 2nd, 3rd, 4th etc.
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text)
    current_year = datetime.now().year

    # Reusable day-name pattern
    _DAY_NAMES = r"Mon(?:day)?|Tue(?:s(?:day)?)?|Wed(?:nesday)?|Thu(?:r(?:s(?:day)?)?)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?"
    _day = rf"(?:{_DAY_NAMES}),?\s*"
    DAY_NAME_PATTERN = rf"\b(?:{_DAY_NAMES})\b,?\s*"

    # ------------------------------------------------------------------
    # 1. "Month D & D, YYYY"  e.g. "March 3 & 22, 2026"
    # ------------------------------------------------------------------
    m_ampersand = re.match(
        r"^([A-Za-z]+)\s+(\d{1,2})\s*&\s*(\d{1,2}),?\s*(\d{4})\s*$", text, flags=re.I
    )
    if m_ampersand:
        try:
            month, start_day, end_day, year = m_ampersand.groups()
            start_date = parser.parse(
                f"{month} {start_day} {year}", dayfirst=False
            ).date()
            end_date = parser.parse(f"{month} {end_day} {year}", dayfirst=False).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning("Failed to parse ampersand date format '%s': %s", text, e)

    # ------------------------------------------------------------------
    # 2. "Month D - Month D, YYYY"  e.g. "Mar 3 - Apr 22, 2026" (cross-month, no day names)
    # ------------------------------------------------------------------
    m_month_range = re.match(
        r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})\s*$",
        text,
        flags=re.I,
    )
    if m_month_range:
        try:
            start_month, start_day, end_month, end_day, year = m_month_range.groups()
            start_date = parser.parse(
                f"{start_month} {start_day} {year}", dayfirst=False
            ).date()
            end_date = parser.parse(
                f"{end_month} {end_day} {year}", dayfirst=False
            ).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning("Failed to parse cross-month date range '%s': %s", text, e)

    # ------------------------------------------------------------------
    # 3. "Month D - D, YYYY"  e.g. "Dec 8 - 13, 2026" (same month, no day names)
    # ------------------------------------------------------------------
    m_same_month_range = re.match(
        r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),?\s*(\d{4})\s*$", text, flags=re.I
    )
    if m_same_month_range:
        try:
            month, start_day, end_day, year = m_same_month_range.groups()
            start_date = parser.parse(
                f"{month} {start_day} {year}", dayfirst=False
            ).date()
            end_date = parser.parse(f"{month} {end_day} {year}", dayfirst=False).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning("Failed to parse same-month date range '%s': %s", text, e)

    # ------------------------------------------------------------------
    # 4. "Day, Month D - Day, Month D, YYYY"
    #    e.g. "Tues, Mar 3 - Sun, Mar 22, 2026"
    #    e.g. "Tues, Dec 8 - Sun, Dec 13, 2026" (day name on both sides)
    # ------------------------------------------------------------------
    m_day_range_both = re.match(
        rf"^{_day}([A-Za-z]+)\s+(\d{{1,2}})\s*-\s*{_day}([A-Za-z]+)\s+(\d{{1,2}}),?\s*(\d{{4}})\s*$",
        text,
        flags=re.I,
    )
    if m_day_range_both:
        try:
            start_month, start_day, end_month, end_day, year = m_day_range_both.groups()
            start_date = parser.parse(
                f"{start_month} {start_day} {year}", dayfirst=False
            ).date()
            end_date = parser.parse(
                f"{end_month} {end_day} {year}", dayfirst=False
            ).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning("Failed to parse day-named date range '%s': %s", text, e)

    # ------------------------------------------------------------------
    # 5. "Day, Month D - Month D, YYYY"
    #    e.g. "Tues, Dec 8 - Dec 13, 2026" (day name only on start side)
    # ------------------------------------------------------------------
    m_day_range_start_only = re.match(
        rf"^{_day}([A-Za-z]+)\s+(\d{{1,2}})\s*-\s*([A-Za-z]+)\s+(\d{{1,2}}),?\s*(\d{{4}})\s*$",
        text,
        flags=re.I,
    )
    if m_day_range_start_only:
        try:
            (
                start_month,
                start_day,
                end_month,
                end_day,
                year,
            ) = m_day_range_start_only.groups()
            start_date = parser.parse(
                f"{start_month} {start_day} {year}", dayfirst=False
            ).date()
            end_date = parser.parse(
                f"{end_month} {end_day} {year}", dayfirst=False
            ).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning(
                "Failed to parse partial day-named date range '%s': %s", text, e
            )

    # ------------------------------------------------------------------
    # 6b. "Day D Month - Day D Month [YYYY]"
    #     e.g. "Thu 11 Feb - Sat 13 Feb 2027" (day name, day num, month on both sides)
    # ------------------------------------------------------------------
    m_day_num_month = re.match(
        rf"^{_day}(\d{{1,2}})\s+([A-Za-z]+)\s*-\s*{_day}(\d{{1,2}})\s+([A-Za-z]+)(?:,?\s*(\d{{4}}))?\s*$",
        text,
        flags=re.I,
    )
    if m_day_num_month:
        try:
            (
                start_day,
                start_month,
                end_day,
                end_month,
                year_str,
            ) = m_day_num_month.groups()
            year = int(year_str) if year_str else current_year
            start_date = parser.parse(
                f"{start_month} {start_day} {year}", dayfirst=False
            ).date()
            end_date = parser.parse(
                f"{end_month} {end_day} {year}", dayfirst=False
            ).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning("Failed to parse day-num-month range '%s': %s", text, e)

    # ------------------------------------------------------------------
    # 6. "Day D - Day D Month [YYYY]"
    #    e.g. "Tue 19 - Sat 23 May" or "Tue 19 - Sat 23 May 2026" (month at end)
    # ------------------------------------------------------------------
    m_day_range_month_last = re.match(
        rf"^{_day}(\d{{1,2}})\s*-\s*{_day}(\d{{1,2}})\s+([A-Za-z]+)(?:,?\s*(\d{{4}}))?\s*$",
        text,
        flags=re.I,
    )
    if m_day_range_month_last:
        try:
            start_day, end_day, month, year_str = m_day_range_month_last.groups()
            year = int(year_str) if year_str else current_year
            start_date = parser.parse(
                f"{month} {start_day} {year}", dayfirst=False
            ).date()
            end_date = parser.parse(f"{month} {end_day} {year}", dayfirst=False).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning("Failed to parse month-last date range '%s': %s", text, e)

    # ------------------------------------------------------------------
    # 6c. "From Month [YYYY]"  e.g. "From November 2025" (open-ended / start-only)
    # ------------------------------------------------------------------
    m_from_month = re.match(
        r"^From\s+(?:\d{1,2}\s+)?([A-Za-z]+)\s+(\d{4})\s*$",
        text,
        flags=re.I,
    )
    if m_from_month:
        try:
            month, year = m_from_month.groups()
            start_date = parser.parse(f"{month} 1 {year}", dayfirst=False).date()
            return {"start_date": start_date.isoformat(), "end_date": None}
        except Exception as e:
            logger.warning("Failed to parse from-month format '%s': %s", text, e)

    # ------------------------------------------------------------------
    # Strip all leading day names before fallback parsing
    # ------------------------------------------------------------------
    text = re.sub(DAY_NAME_PATTERN, "", text, flags=re.I).strip()

    # ------------------------------------------------------------------
    # 7. Single date  e.g. "March 3" or "March 3, 2026"
    # ------------------------------------------------------------------
    m_single = re.match(r"^\s*([A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?)\s*$", text)
    if m_single:
        try:
            single_date = parser.parse(m_single.group(1), dayfirst=None).date()
            return {
                "start_date": single_date.isoformat(),
                "end_date": single_date.isoformat(),
            }
        except Exception as e:
            logger.warning("Failed to parse single date '%s': %s", m_single.group(1), e)

    # ------------------------------------------------------------------
    # 8. "from X to Y"
    # ------------------------------------------------------------------
    m_from_to = re.search(
        r"from\s+([A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?)\s+to\s+([A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?)",
        text,
        flags=re.I,
    )
    if m_from_to:
        try:
            start_date = parser.parse(m_from_to.group(1), dayfirst=None).date()
            end_date = parser.parse(m_from_to.group(2), dayfirst=None).date()
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        except Exception as e:
            logger.warning(
                "Failed to parse 'from...to' date range in '%s': %s", text, e
            )

    # ------------------------------------------------------------------
    # 9. Generic fallback: split on "and", handle "-" ranges
    # ------------------------------------------------------------------
    if not re.search(r"\d{4}", text):
        text = re.sub(r"(\d{1,2}\s+[A-Za-z]+)", rf"\1 {current_year}", text)

    start_date, end_date = None, None
    all_starts, all_ends = [], []
    try:
        parts = re.split(r"\band\b", text, flags=re.I)
        for part in parts:
            part = re.sub(DAY_NAME_PATTERN, "", part.strip(), flags=re.I)
            part_start, part_end = None, None

            if "-" in part and not re.search(r"\s-\s", part):
                part = part.replace("-", " - ")

            if re.search(r"\s-\s", part):
                left, right = re.split(r"\s-\s", part, maxsplit=1)
                left = re.sub(
                    DAY_NAME_PATTERN, "", left.strip().rstrip(","), flags=re.I
                )
                right = re.sub(
                    DAY_NAME_PATTERN, "", right.strip().rstrip(","), flags=re.I
                )
                # If left has no year but right does, inherit it so start
                # doesn't default to current_year when end is a future year.
                _left_has_year = bool(re.search(r"\d{4}", left))
                _right_year_m = re.search(r"\d{4}", right)
                if not _left_has_year and _right_year_m:
                    left = f"{left} {_right_year_m.group()}"
                part_start = parser.parse(left, dayfirst=None).date()
                if not re.search(r"[a-zA-Z]", right):
                    right = f"{part_start.strftime('%b')} {right}"
                if not re.search(r"\d{4}", right):
                    right = f"{right} {part_start.year}"
                part_end = parser.parse(right, dayfirst=True).date()
            elif re.search(r"\d{1,2}\s\w+\s\d{4}", part):
                part_start = parser.parse(part, dayfirst=None).date()
                part_end = part_start
            elif re.search(r"(until|to)", part, re.I):
                part_end = parser.parse(part, dayfirst=None).date()
            elif re.search(r"(start booking|currently booking)", part, re.I):
                part_start = parser.parse(part, dayfirst=None, fuzzy=True).date()

            if part_start:
                all_starts.append(part_start)
            if part_end:
                all_ends.append(part_end)

        if all_starts:
            start_date = min(all_starts)
        if all_ends:
            end_date = max(all_ends)

        return {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        }
    except Exception as e:
        logger.warning("Failed to parse booking dates '%s': %s", text, e)
        return {"start_date": None, "end_date": None}


def human_scroll(sb):
    """Non-linear scrolling to satisfy behavioral sensors (Reese84)."""
    try:
        for _ in range(random.randint(2, 4)):
            # Random scroll distance
            delta = random.randint(250, 700)
            sb.execute_script(f"window.scrollBy(0, {delta});")
            # Jittery pause between scrolls
            time.sleep(random.uniform(0.6, 1.4))
    except Exception:
        pass


def get_city_country_uk(postcode):
    try:
        postcode = postcode.replace(" ", "")
        url = f"https://api.postcodes.io/postcodes/{postcode}"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            city = data["result"]["admin_district"]
            country = "United Kingdom"
            return city, country
        return None, None
    except Exception:
        return None, None


def get_venue_location_geopy(venue_name, user_agent="ovation_scraper"):
    """Get address, city, and country for a venue using geopy.

    Args:
        venue_name: Name of the venue to geocode
        user_agent: User agent string for Nominatim API

    Returns:
        dict: Dictionary with 'full_address', 'city', 'country' keys or None

    Examples:
        >>> get_venue_location_geopy("Gran Teatro Falla, Cádiz")
        {'full_address': 'Gran Teatro Falla, Plaza Fragela, ...',
         'city': 'Cádiz',
         'country': 'Spain'}
        >>> get_venue_location_geopy("Nonexistent Venue 12345")
        None
        >>> get_venue_location_geopy(None)
        None
    """
    if not venue_name:
        return None

    try:
        geolocator = Nominatim(user_agent=user_agent)
        location = geolocator.geocode(venue_name)

        if location:
            address = location.address
            address_parts = [part.strip() for part in address.split(",")]
            city = address_parts[1] if len(address_parts) > 1 else None
            country_raw = address_parts[-1] if len(address_parts) > 0 else None

            # Handle Spanish country names
            if country_raw and country_raw.lower() in ["españa", "espana"]:
                country = "Spain"
            else:
                country = normalize_country(country_raw) if country_raw else None

            return {"full_address": address, "city": city, "country": country}
        logger.warning(f"Geopy could not find location for: {venue_name}")
        return None
    except Exception as e:
        logger.error(f"Error getting location for {venue_name}: {e}")
        return None


def format_datetime_key(date_input, time_str):
    """Format date and time into standardized datetime key (YYYY-MM-DD HH:MM).

    Args:
        date_input: Date string, datetime object, or None
        time_str: Time string in HH:MM format

    Returns:
        str: Formatted datetime key "YYYY-MM-DD HH:MM" or None

    Examples:
        >>> format_datetime_key("20/12/2024", "19:00")
        '2024-12-20 19:00'
        >>> format_datetime_key("2024-12-20", "14:30")
        '2024-12-20 14:30'
        >>> format_datetime_key(datetime(2024, 12, 20), "19:00")
        '2024-12-20 19:00'
        >>> format_datetime_key(None, "19:00")
        None
    """
    if not date_input or not time_str:
        return None

    date_iso = format_date_to_iso(date_input)
    if not date_iso:
        return None

    return f"{date_iso} {time_str}"


def get_genre_from_google(title, search_url="https://www.google.com/search?q="):
    """Search Google for show genre and return category.

    Args:
        title: Show title to search for
        search_url: Google search URL (default: https://www.google.com/search?q=)

    Returns:
        str: Genre category (capitalized) or None

    Examples:
        >>> get_genre_from_google("Hamilton")
        'Musical'
        >>> get_genre_from_google("Hamlet")
        'Play'
        >>> get_genre_from_google("Swan Lake")
        'Ballet'
        >>> get_genre_from_google(None)
        None
    """
    if not title:
        return None

    # Common genre keywords to search for
    genre_keywords = [
        "musical",
        "play",
    ]

    try:
        search_query = f"{title} genre"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(
            f"{search_url}{search_query}", headers=headers, timeout=5
        )

        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text().lower()

        # Search first 500 characters for genre keywords
        for genre in genre_keywords:
            if genre in text[:500]:
                return standardize_category(genre)

        return None
    except Exception:
        return None


def get_scrape_datetime() -> str:
    """Return the current datetime as a formatted string.

    Returns:
        str: Current datetime in 'YYYY-MM-DD HH:MM' format.

    Examples:
        >>> get_scrape_datetime()
        '2026-03-13 14:30'
        >>> record = {"scrape_datetime": get_scrape_datetime()}
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def extract_price_from_text(text, currency_symbol="$"):
    """Extract price value and type from text string.

    Args:
        text: Raw text containing price information
        currency_symbol: Currency symbol to search for (default: "$")

    Returns:
        tuple: (price_value, price_type) where price_type is "from", "exact", or ""

    Usage:
        >>> extract_price_from_text("From $45 available")
        (45, "from")
        >>> extract_price_from_text("$30 tickets")
        (30, "exact")
        >>> extract_price_from_text("No price listed")
        (None, "")
    """
    if not text:
        return None, ""

    pattern = rf"(From\s*)?\{currency_symbol}(\d+)"
    price_m = re.search(pattern, text, re.IGNORECASE)

    if not price_m:
        return None, ""

    price_value = float(price_m.group(2))
    price_type = "from" if price_m.group(1) else "exact"

    return price_value, price_type


def get_city_country_from_postcode(postcode, country_code="FR"):
    """
    Use Zippopotam.us API to get city and country from postcode
    country_code: ISO 2-letter country code, 'FR' for France
    """
    try:
        url = f"https://api.zippopotam.us/{country_code}/{postcode}"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            city = data["places"][0]["place name"]
            city = city.split(" ")[0]  # Take only "Paris"
            country = data["country"]
            return city, country
        else:
            return None, None
    except Exception as e:
        print("Error:", e)
        return None, None


def get_city_country_us(address):
    """Get city and country from a US address using existing helpers."""
    postcode = extract_postcode_US(address)
    if not postcode:
        return None, None

    # Use the 5-digit ZIP only (strip ZIP+4 if present)
    zip5 = postcode.split("-")[0]

    city, country = get_city_country_from_postcode(zip5, country_code="US")
    return city, country
