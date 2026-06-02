import re
import os
import time
import logging
import traceback
import pandas as pd

from datetime import datetime, date
from dateutil import parser

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import undetected_chromedriver as uc


# ============================================================
# CONFIG
# ============================================================
RUN_HEADLESS = True
OUTPUT_FILE = "output.csv"

PAGES = [
    ("https://watfordpalacetheatre.co.uk/whats-on/?category=musical",
        "Musical"
    ),
    ("https://watfordpalacetheatre.co.uk/whats-on/?category=drama",
        "Play"
    ),
]


# ============================================================
# LOGGING
# ============================================================
if not os.path.exists("log"):
    os.makedirs("log")

logging.basicConfig(
    filename="log/scrape.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def log(msg, level="info"):
    print(f"[LOG] {msg}")

    if level == "error":
        logging.error(msg)
    elif level == "warning":
        logging.warning(msg)
    else:
        logging.info(msg)


# ============================================================
# BROWSER
# ============================================================
def setup_browser():

    log("🚀 Starting browser...")

    options = uc.ChromeOptions()

    if RUN_HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = uc.Chrome(options=options, version_main=147)

    log("✅ Browser ready")

    return driver


# ============================================================
# SAFE GET
# ============================================================
def safe_get(driver, url, retries=3):

    for attempt in range(1, retries + 1):

        try:
            log(f"🌍 Loading page ({attempt}/{retries}): {url}")
            driver.get(url)
            log("✅ Page loaded")
            return True

        except Exception as e:
            log(f"❌ Load failed: {e}", "error")
            time.sleep(2)

    return False


# ============================================================
# SCROLL
# ============================================================
def scroll_to_load_all(driver):

    log("⬇️ Scrolling page...")

    last_height = driver.execute_script("return document.body.scrollHeight")

    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        new_height = driver.execute_script("return document.body.scrollHeight")

        if new_height == last_height:
            break

        last_height = new_height

    log("✅ Finished scrolling")


# ============================================================
# DATE PARSER
# ============================================================
def parse_date(text):

    try:
        dt = parser.parse(text, dayfirst=True, fuzzy=True)
        if not dt:
          log(f"unable to parse date")

        if dt.date() < date.today():
            dt = dt.replace(year=dt.year + 1)
        return dt.strftime("%Y-%m-%d")
    except Exception as e:
        return log(f"date parse error: {e}", "warning")


# ============================================================
# CLEAN CURRENCY TEXT
# ============================================================
def detect_currency(text):
    if not text:
        return None

    if "£" in text:
        return "GBP"
    elif "$" in text:
        return "USD"
    elif "€" in text:
        return "EUR"
    elif "₦" in text:
        return "NGN"

    return None


# ============================================================
# EVENTS
# ============================================================
def extract_events(driver, category):

    log(f"🎭 Extracting events for category: {category}")

    cards = driver.find_elements(By.CSS_SELECTOR, "div.gridblock.postitem")
    log(f"📦 Found {len(cards)} event cards")

    events = []
    for i, card in enumerate(cards, start=1):

        try:
            title = card.find_element(By.CSS_SELECTOR, "h2.entry-title").text.strip()
            url = card.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
            
            log(f"   ➤ [{i}/{len(cards)}] {title}")

            if not url.startswith("http"):
                continue

            text_blob = " ".join([
                p.get_attribute("textContent") or ""
                for p in card.find_elements(By.TAG_NAME, "p")
            ])

            events.append({
                "title": title,
                "venue_url": url,
                "category": category,
                "currency": detect_currency(text_blob)
            })

        except Exception as e:
            log(f"⚠️ Event parse error: {e}", "warning")

    log(f"✅ Total events extracted: {len(events)}")

    return events


# ============================================================
# EVENT DETAILS
# ============================================================
def extract_event_details(driver):

    log("🔎 Extracting event details...")

    data = {
        "upcoming_performances": [],
        "seat_pricing": {},
        "venue": None,
        "address": None,
        "city": None,
        "country": None,
        "is_limited_run": False,
        "open_date": None,
        "close_date": None
    }

    performances = []

    # ---------------- VENUE ----------------
    try:
        footer = driver.find_element(By.CSS_SELECTOR, "p.footeraddress")
        lines = [x.strip() for x in footer.text.split("\n") if x.strip()]

        if lines:
            data["venue"] = lines[0].replace(",", "").strip()

            if len(lines) > 1:
                parts = [p.strip() for p in lines[1].split(",")]
                if len(parts) > 0:
                    data["address"] = parts[0]
                if len(parts) > 1:
                    data["city"] = parts[1]

            if ".co.uk" in driver.current_url:
                data["country"] = "United Kingdom"

    except Exception as e:
        log(f"Venue error: {e}", "warning")

    # ---------------- PERFORMANCES ----------------

    try:
        WebDriverWait(driver, 20).until(
            lambda d: len(
                d.find_elements(By.CSS_SELECTOR, ".spektrix_booking--event")
            ) > 0
        )

        time.sleep(3)

        blocks = driver.find_elements(By.CSS_SELECTOR, "div.spektrix_booking--event")

        log(f"🎟 Found {len(blocks)} performances")

        for idx, block in enumerate(blocks, start=1):

            try:
                raw_date = block.find_element(
                    By.CSS_SELECTOR,
                    ".spektrix_booking--date"
                ).text.strip()

                raw_time = block.find_element(
                    By.CSS_SELECTOR,
                    ".spektrix_booking--time"
                ).text.strip()

                booking_url = block.find_element(
                    By.CSS_SELECTOR,
                    "a.button"
                ).get_attribute("href")

                # 🔥 CLEAN VALIDATION (ONLY ONCE)
                if not raw_date or not raw_time:
                    log(f"⚠️ Skipping empty performance block {idx}", "warning")
                    continue

                date_text = parse_date(raw_date)

                time_text = parser.parse(
                    raw_time,
                    fuzzy=True
                ).strftime("%H:%M")

                perf = {
                    "date": date_text,
                    "time": time_text,
                    "booking_url": booking_url
                }

                performances.append(perf)

                log(f"   🎫 Perf {idx}: {perf['date']} {perf['time']}")

            except Exception as inner_e:
                log(f"⚠️ Skipping performance {idx}: {inner_e}", "warning")

        data["upcoming_performances"] = performances

    except Exception as e:
        log(f"Performance error: {e}", "warning")



    # ---------------- LIMITED RUN ----------------
    try:
      for perf in performances:
        open_date  = performances[0]["date"]
        close_date = performances[-1]["date"]

        data["open_date"] = open_date
        data["close_date"] = close_date

        if open_date and close_date:
            data["is_limited_run"] = (
                (parser.parse(close_date) - parser.parse(open_date)).days <= 21
            )

    except Exception as e:
      log(f"limited run error: {e}", "warning")


    # ========================================================
    # SEAT PRICING
    # ========================================================
    seat_pricing = {}

    log("💺 Starting seat extraction...")

    for i, perf in enumerate(performances, start=1):
        
        try:
            start = time.time()

            log(f"   🔄 [{i}/{len(performances)}] {perf['date']} {perf['time']}")

            driver.get(perf["booking_url"])

            iframe = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "SpektrixIFrame"))
            )

            driver.switch_to.frame(iframe)

            seats = WebDriverWait(driver, 10).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "img.SeatSelectable"))
            )

            seat_list = []

            for seat in seats:

                tooltip = seat.get_attribute("tooltip") or seat.get_attribute("title") or ""

                match = re.search(r"([A-Z]+\d+)\s*-\s*£?([\d,.]+)", tooltip)

                if match:
                    seat_list.append({
                        "seat": match.group(1),
                        "ticket_price": float(match.group(2).replace(",", ""))
                    })

            perf["capacity"] = len(seats)

            key = f"{perf['date']} {perf['time']}"

            seat_pricing[key] = seat_list

            log(f"      ✅ Seats: {len(seat_list)} | Time: {round(time.time()-start,2)}s")

        except Exception as e:
            log(f"❌ Seat error: {e}", "warning")

        finally:
            try:
                driver.switch_to.default_content()
            except:
                pass

    data["seat_pricing"] = seat_pricing

    log("✅ Seat extraction complete")

    return data


# ============================================================
# MAIN
# ============================================================
def scrape_shows():

    log("🚀 SCRAPER STARTED")

    driver = setup_browser()
    all_rows = []

    for page_idx, (url, category) in enumerate(PAGES, start=1):

        log(f"\n🌍 PAGE {page_idx}/{len(PAGES)} → {category}")

        if not safe_get(driver, url):
            continue

        scroll_to_load_all(driver)

        events = extract_events(driver, category)

        for i, e in enumerate(events, start=1):

            log(f"\n🎭 EVENT {i}/{len(events)} → {e['title']}")

            if not safe_get(driver, e["venue_url"]):
                continue

            details = extract_event_details(driver)
       

            row = {

                "title": e["title"],
                "venue_url": e["venue_url"],
                "category": category,

                "venue": details["venue"] or "UNKNOWN",
                "address": details["address"] or "UNKNOWN",
                "city": details["city"] or "UNKNOWN",
                "country": details["country"] or "UNKNOWN",

                "open_date": details["open_date"],
                "close_date": details["close_date"],

                "booking_start_date": details["open_date"],
                "booking_end_date": details["close_date"],

                "upcoming_performances": str([
                    {"date": p["date"], "time": p["time"]}
                    for p in details["upcoming_performances"]
                ]),

                "capacity": max([p.get("capacity", 0) for p in details["upcoming_performances"]] + [0]),

                "currency": e["currency"] or "GBP",

                "is_limited_run": bool(details["is_limited_run"]),

                "seat_pricing": str(details["seat_pricing"]),

                "scrape_datetime": datetime.now().strftime("%Y-%m-%d %H:%M")
            }

            all_rows.append(row)

            log(f"✅ Saved: {e['title']}")

    df = pd.DataFrame(all_rows)

    df.to_csv(OUTPUT_FILE, index=False)

    log("🎉 SCRAPING COMPLETE")

    driver.quit()


if __name__ == "__main__":
    scrape_shows()
