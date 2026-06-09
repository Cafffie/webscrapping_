import re
import os
import time
import logging
import pandas as pd
from datetime import datetime
from dateutil import parser

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import undetected_chromedriver as uc

# ============================================================
# CONFIG & LOGGING
# ============================================================
RUN_HEADLESS = False
OUTPUT_FILE = "output.csv"
PAGES = [
    ("https://elgiva.com/book-a-show/musical-theatre/", "Musical"),
    ("https://elgiva.com/book-a-show/theatre/", "Play")
]

if not os.path.exists("log"):
    os.makedirs("log")

logging.basicConfig(
    filename="log/scrape.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

def log(msg, level="info"):
    print(f"[LOG] {msg}")
    if level == "error": logging.error(msg)
    elif level == "warning": logging.warning(msg)
    else: logging.info(msg)


# ============================================================
# BROWSER SETUP
# ============================================================
def setup_browser():
    log("🚀 Starting browser...")
    options = uc.ChromeOptions()
    if RUN_HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    
    # Removed hardcoded version_main=147 to allow automatic environment matching
    driver = uc.Chrome(options=options, version_main=147)
    driver.implicitly_wait(10)
    return driver


def safe_get(driver, url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            log(f"🌍 Loading page ({attempt}/{retries}): {url}")
            driver.get(url)
            return True
        except Exception as e:
            log(f"❌ Load failed: {e}", "error")
            time.sleep(2)
    return False


def handle_cookies(driver):
    try:
        cookie_btn_selector = "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"
        WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, cookie_btn_selector))
        )
        driver.find_element(By.CSS_SELECTOR, cookie_btn_selector).click()
        log("Cookies accepted.")
        time.sleep(1) 
    except TimeoutException:
        pass 


def scroll_to_load_all(driver):
    log("⬇️ Scrolling page...")
    last_height = driver.execute_script("return document.body.scrollHeight")

    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)

        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    log("✅ Finished scrolling")


# ============================================================
# CLEAN CURRENCY TEXT
# ============================================================
def detect_currency(text):
    if not text: return None
    if "£" in text: return "GBP"
    elif "$" in text: return "USD"
    elif "€" in text: return "EUR"
    return None


# ============================================================
# 1. VENUE DETAILS FUNCTION
# ============================================================
def get_venue_details(driver):
    """Extracts venue structural details dynamically from the page footer."""
    details = {
        "venue": "The Elgiva",
        "address": "St Mary’s Way",
        "city": "Chesham",
        "country": "UK"
    }
    try:
        footer_p = driver.find_element(By.XPATH, "//footer//p[contains(text(), 'Buckinghamshire')]")
        text_lines = footer_p.text.split('\n')
        if len(text_lines) > 1:
            address_parts = text_lines[-1].split(',')
            if len(address_parts) >= 3:
                details["address"] = address_parts[0].strip()
                details["city"] = address_parts[1].strip()
    except Exception:
        pass
        
    return details


# ============================================================
# 2. EVENT LIST SELECTION
# ============================================================
def extract_event_list(driver, category):
    """Extracts parent card links from landing grids to build routing lists."""
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "article.elementor-post"))
    )
    shows = driver.find_elements(By.CSS_SELECTOR, "article.elementor-post")
    show_links = []
    
    for show in shows:
        try:
            link_el = show.find_element(By.CSS_SELECTOR, "h2.elementor-post__title a")
            show_links.append({
                "title": link_el.text.strip(),
                "event_url": link_el.get_attribute("href"),
                "category": category
            })
        except Exception:
            continue
            
    return show_links


# ============================================================
# 3. PERFORMANCE TIMELINE PROCESSING
# ============================================================
def clean_time_string(time_element):
    """Converts mixed presentation timestamps (e.g., '4:30pm') to explicit ISO 24-hour style."""
    try:
        raw_text = time_element.text.strip().lower()
        span_hour_min = time_element.find_element(By.CSS_SELECTOR, "span").text.strip()
        
        match = re.search(r"(\d+):(\d+)", span_hour_min)
        if not match:
            return ""
        hour = int(match.group(1))
        minute = int(match.group(2))
        
        if "pm" in raw_text and hour < 12:
            hour += 12
        elif "am" in raw_text and hour == 12:
            hour = 0
            
        return f"{hour:02d}:{minute:02d}"
    except Exception:
        return ""


def extract_performances_from_table(driver):
    """Parses performance listing matrices from tables using date row class identifiers."""
    perf_list = []
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "tr[class*='dot_events_day_']")
        for row in rows:
            class_attr = row.get_attribute("class") or ""
            date_match = re.search(r"dot_events_day_(\d{4})(\d{2})(\d{2})", class_attr)
            if not date_match:
                continue
                
            iso_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
            perf_links = row.find_elements(By.CSS_SELECTOR, "td a")
            
            for link in perf_links:
                try:
                    iso_time = clean_time_string(link)
                    if not iso_time:
                        continue
                        
                    title_attr = (link.get_attribute("title") or "").lower()
                    # FIXED: Added fallback (or "") to prevent NoneType TypeError crash
                    class_string = link.get_attribute("class") or ""
                    is_sold_out = "sold out" in title_attr or "dot_events_sold_out" in class_string
                    
                    perf_list.append({
                        "date": iso_date,
                        "time": iso_time,
                        "booking_url": link.get_attribute("href") if not is_sold_out else "",
                        "sold_out": is_sold_out
                    })
                except Exception as inner_e:
                    log(f"⚠️ Skipping individual performance entry error: {inner_e}", "warning")
    except Exception as e:
        log(f"⚠️ Error locating performance matrices elements: {e}", "warning")
        
    return perf_list


# ============================================================
# SEAT PRICING
# ============================================================
def extract_all_seats(driver, performances):
    """Extracts seats and pricing from the currently open SVG modal."""

    log("💺 Extracting seats from all seat map sections...")

    seat_pricing = {}
    currency = None

    for i, perf in enumerate(performances, start=1):

        try:
            start = time.time()

            log(f"   🔄 [{i}/{len(performances)}] {perf['date']} {perf['time']}")

            driver.get(perf["booking_url"])

            iframe = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "SpektrixIFrame"))
            )

            driver.switch_to.frame(iframe)

            WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.SeatingArea img"))
            )
            
            seats = driver.find_elements(By.CSS_SELECTOR, "div.SeatingArea img")
            log(f"📦 Found {len(seats)} unique seats in this section")
            
            seat_list = []
            for seat in seats:
                tooltip = seat.get_attribute("tooltip") or seat.get_attribute("title") or ""
                if not tooltip:
                            continue
                
                match = re.search(r"([A-Z]+\d+)\s*-\s*£?([\d,.]+)", tooltip)                 
                if not match:
                            continue
                seat_id = match.group(1)
                ticket_price = float(match.group(2))

                if tooltip:
                    seat_list.append({
                        "seat": seat_id,
                        "ticket_price": ticket_price
                    })

            perf["capacity"] = len(seats)
            if not currency:
                perf["currency"] = detect_currency(tooltip)
        
            key = f"{perf['date']} {perf['time']}"
            seat_pricing[key] = seat_list

            log(f" ✅ Seats: {len(seats)} | Time: {round(time.time()-start,2)}s")

        except Exception as e:
            log(f"❌ Seat extraction error: {e}", "warning")

        finally:
            try:
                driver.switch_to.default_content()
            except:
                pass

    log("✅ Seat extraction complete")

    return seat_pricing


# ============================================================
# MAIN APPLICATION FLOW
# ============================================================
def scrape_shows():
    log("🚀 SCRAPER STARTED")

    driver = setup_browser()
    all_rows = []

    try:
        for page_idx, (url, category) in enumerate(PAGES, start=1):
            log(f"\n🌍 CATEGORY CORRELATION {page_idx}/{len(PAGES)} → {category}")

            if not safe_get(driver, url):
                continue

            handle_cookies(driver)
            scroll_to_load_all(driver)
            shows = extract_event_list(driver, category)

            for i, show in enumerate(shows[:2], start=1):
                log(f"\n🎭 EVENT SPECIFIC EXTRACTION {i}/{len(shows)} → {show['title']}")

                if not safe_get(driver, show["event_url"]):
                    continue

                handle_cookies(driver)
                scroll_to_load_all(driver)
                scrape_dt = datetime.now().strftime("%Y-%m-%d %H:%M")
                venue_details = get_venue_details(driver)

                raw_performances = extract_performances_from_table(driver)
                if not raw_performances:
                    log(f"⚠️ No active performances extracted for '{show['title']}', row skipped.")
                    continue

                performance_dates = [p["date"] for p in raw_performances]
                open_date = min(performance_dates) if performance_dates else ""
                close_date = max(performance_dates) if performance_dates else ""
                
                formatted_performances = str([
                    {"date": p["date"], "time": p["time"]} for p in raw_performances
                ])

                seat_pricing = extract_all_seats(driver, raw_performances)
                formatted_seat_pricing = repr(seat_pricing) if seat_pricing else "{}"
                
                capacity = max([p.get("capacity", 0) for p in raw_performances], default=0)

                # Find the first performance that successfully extracted a currency string, fallback to None
                currency = next((p.get("currency") for p in raw_performances if p.get("currency")), None)

                row = {
                    "title": show["title"],
                    "venue_url": show["event_url"],
                    "category": show["category"],
                    "venue": venue_details["venue"],
                    "address": venue_details["address"],
                    "city": venue_details["city"],
                    "country": venue_details["country"],
                    "open_date": open_date,
                    "close_date": close_date,
                    "booking_start_date": open_date, 
                    "booking_end_date": close_date,
                    "upcoming_performances": formatted_performances,
                    "capacity": capacity,        # Fixed: Explicitly declared empty string instead of commented out
                    "currency": currency,        # Fixed: Explicitly declared empty string instead of commented out
                    "is_limited_run": True,
                    "seat_pricing": formatted_seat_pricing,  # Fixed: Initialized to valid minimal JSON string literal pattern matching rules
                    "scrape_datetime": scrape_dt
                }
                all_rows.append(row)
                log(f"✅ Extracted Row Record Saved: {show['title']}")

    except Exception as e:
        log(f"⚠️ Error occurred while scraping shows: {e}", "warning")

    finally:
        driver.quit()
        log("🛑 Browser processes completely shut down.")

    # Build CSV in strict canonical order
    canonical_columns = [
        "title", "venue_url", "category", "venue", "address", "city", "country",
        "open_date", "close_date", "booking_start_date", "booking_end_date",
        "upcoming_performances", "capacity", "currency", "is_limited_run",
        "seat_pricing", "scrape_datetime"
    ]

    if all_rows:
        df = pd.DataFrame(all_rows)
        df = df.reindex(columns=canonical_columns)
    else:
        df = pd.DataFrame(columns=canonical_columns)

    df.to_csv(OUTPUT_FILE, index=False)
    log(f"✅ Scraped data saved to: {OUTPUT_FILE} ({len(df)} lines generated).")


if __name__ == "__main__":
    scrape_shows()
