import re
import os
from urllib.parse import urljoin
import traceback
import time
import json
import hashlib
import logging
import pandas as pd
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException, ElementClickInterceptedException
from selenium.webdriver.common.action_chains import ActionChains
import undetected_chromedriver as uc
import random
from bs4 import BeautifulSoup
from dateutil import parser


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
RUN_HEADLESS = True
BASE_URL = "https://www.todaytix.com"

if not os.path.exists("log"):
    os.makedirs("log")
logging.basicConfig(
    filename="log/scrape.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ------------------------------------------------------------
# UTILITIES
# ------------------------------------------------------------
def log_and_print(message):
    print(message)
    logging.info(message)


def setup_browser():
    """Setup undetected Chrome with proper settings."""
    options = uc.ChromeOptions()
    if RUN_HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-infobars")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--lang=en-US,en;q=0.9")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.188 Safari/537.36"
    )
    try:
        return uc.Chrome(options=options, version_main=145)
    except Exception as e:
        log_and_print(f"Driver error: {e}. Trying without explicit version...")
        return uc.Chrome(options=options)


def standardize_title(title: str):
    if not title:
        return None
    return re.split(r"[:\-–—!|]", title)[0].strip()


# ------------------------------------------------------------
# PAGE SCROLLING
# ------------------------------------------------------------
def scroll_to_load_all_shows(driver):
    """Scroll down until no new content loads."""
    log_and_print("🔽 Scrolling to load all shows...")
    last_height = driver.execute_script("return document.body.scrollHeight")
    pause = random.uniform(2, 4)

    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    log_and_print("✅ Finished scrolling.")


# ------------------------------------------------------------
# SHOW LIST EXTRACTION
# ------------------------------------------------------------
def extract_shows_from_page(driver, market):
    """Extract shows (title, cover image, link) from listing page."""
    shows_list = []

    show_cards = driver.find_elements(By.CSS_SELECTOR, "div[data-test-id^='poster-']")
    log_and_print(f"🎭 Found {len(show_cards)} shows.")

    for show in show_cards:
        try:
            title = show.find_element(By.CSS_SELECTOR, "p[data-test-id^='product-']").text.strip()
        except:
            title = None

        try:
            show_website = show.find_element(By.TAG_NAME, "a").get_attribute("href")
        except:
            show_website = None

        try:
            image_url = show.find_element(By.CSS_SELECTOR, "img").get_attribute("src")
        except:
            image_url = None

        shows_list.append({
            "title": title,
            "standardized_title": standardize_title(title),
            "show_website": show_website,
            "image_url": image_url,
            "market": market,
            "category": "show-production",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    return shows_list


# ------------------------------------------------------------
# DETAIL PAGE EXTRACTION
# ------------------------------------------------------------
def extract_description(driver):
    """Scrape show description if available."""
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "section#about-content"))
        )

        try:
            read_btn = driver.find_element(By.CSS_SELECTOR, "button[data-test-id='about-expand']")
            driver.execute_script("arguments[0].click();", read_btn)
        except:
            pass

        about_section = driver.find_element(By.CSS_SELECTOR, "section#about-content")
        paragraphs = about_section.find_elements(By.TAG_NAME, "p")
        return "\n\n".join(
            p.get_attribute("textContent").strip()
            for p in paragraphs if p.get_attribute("textContent").strip()
        )

    except Exception as e:
        log_and_print(f"❌ Description error: {e}")
        return None


def extract_venue(driver):
    try:
        venue = driver.find_element(By.CSS_SELECTOR, "div[data-test-id='section-Venue'] p a").text
        return re.sub(r"\btheatre\b", "", venue, flags=re.IGNORECASE).strip()
    except:
        return None


def extract_type(driver):
    try:
        categories_p = driver.find_element(By.CSS_SELECTOR, "div[data-test-id='section-Categories'] p")
        links = categories_p.find_elements(By.TAG_NAME, "a")
        return ", ".join([l.text.strip().lower() for l in links])
    except:
        return "N/A"


# ------------------------------------------------------------
# CALENDAR SCRAPING
# ------------------------------------------------------------
def calendar_click_next_month(driver):
    """Try click next month button if available."""
    try:
        next_btn = driver.find_element(By.CSS_SELECTOR, "#show-calendar button[aria-label='Next month']")
        if next_btn.get_attribute("disabled"):
            return False

        driver.execute_script("arguments[0].click();", next_btn)
        time.sleep(1.2)
        return True

    except:
        return False


def extract_dates_on_calendar(driver):
    """Return list of active date labels."""
    abbrs = driver.find_elements(By.CSS_SELECTOR, "#show-calendar button:not([disabled]) abbr")
    return [a.get_attribute("aria-label") for a in abbrs if a.get_attribute("aria-label")]


# ------------------------------------------------------------
# PERFORMANCE SCRAPING
# ------------------------------------------------------------
def extract_performances_for_date(driver, item, parsed_date, formatted_date):
    """Extract showtimes on a selected date."""
    performances = []

    try:
        WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#showtimes-list"))
        )
    except TimeoutException:
        pass

    showtimes = driver.find_elements(By.CSS_SELECTOR, "#showtimes-list span.t-time")
    if not showtimes:
        log_and_print(f"⚠️ No showtimes for {formatted_date}")
        return performances

    day_name = parsed_date.strftime("%A")
    is_canceled = "No" if formatted_date else "Yes"

    for span in showtimes:
        perf_time = span.get_attribute("textContent").strip()

        status = "upcoming" if parsed_date.date() > datetime.now().date() else "active"


    prices = driver.find_elements(By.CSS_SELECTOR, "#showtimes-list div.t-showtimes.price")
    if not prices:
        log_and_print(f"⚠️ No prices for {formatted_date}")
        return performances

    for div in prices:
        price = div.get_attribute("textContent").strip()

        perf = {
            **item,
            "performance_date": formatted_date,
            "day_of_week": day_name,
            "time": perf_time,
            "price": price,
            "status": status,
            "is_canceled": is_canceled,
        }
        performances.append(perf)

    return performances


def scrape_calendar(driver, item):
    """Scrape all dates + performance times for one show."""
    performance_list = []
    seen_dates = set()

    while True:
        dates = extract_dates_on_calendar(driver)
        new_dates = [d for d in dates if d not in seen_dates]

        if not new_dates:
            if not calendar_click_next_month(driver):
                break
            continue

        for date_label in new_dates:
            seen_dates.add(date_label)

            try:
                parsed_date = parser.parse(date_label)
                formatted_date = parsed_date.strftime("%Y-%m-%d")
            except:
                continue

            xpath = f"//div[@id='show-calendar']//button[.//abbr[@aria-label=\"{date_label}\"]]"
            try:
                btn = driver.find_element(By.XPATH, xpath)
                driver.execute_script("arguments[0].click();", btn)
            except:
                continue

            time.sleep(0.8)
            perfs = extract_performances_for_date(driver, item, parsed_date, formatted_date)
            performance_list.extend(perfs)

    return performance_list


# ------------------------------------------------------------
# MAIN SCRAPER
# ------------------------------------------------------------
def scrape_shows():
    driver = setup_browser()
    all_performances = []
    all_shows = []

    pages = [
        ("https://www.todaytix.com/brisbane/category/all-shows", "Australia"),
    ]

    try:
        for url, market in pages:
            driver.get(url)
            time.sleep(3)
            scroll_to_load_all_shows(driver)

            shows = extract_shows_from_page(driver, market)
            all_shows.extend(shows)

            # visit each show page
            for item in shows:
                if not item["show_website"]:
                    continue

                driver.get(item["show_website"])
                time.sleep(2)

                item["description"] = extract_description(driver)
                item["theatre"] = extract_venue(driver)
                item["type"] = extract_type(driver)

                try:
                    performances = scrape_calendar(driver, item)
                    all_performances.extend(performances)
                except Exception as e:
                    log_and_print(f"Calendar error: {e}")

        if all_performances:
            pd.DataFrame(all_performances).to_csv("data4.csv", index=False)
            log_and_print(f"Saved {len(all_performances)} rows")

    finally:
        try:
            driver.quit()
        except:
            pass


# ------------------------------------------------------------
# RUN
# ------------------------------------------------------------
if __name__ == "__main__":
    scrape_shows()
