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
BASE_URL = "https://keegantheatre.com"

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

# ------------------------------------------------------------
# PAGE SCROLLING
# ------------------------------------------------------------
def scroll_to_load_all_shows(driver):
    log_and_print("🌐 Navigating to the website page...")
    time.sleep(random.uniform(2, 4))

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div[class^='team-']"))
        )
        log_and_print("✅ Base listings container loaded.")
    except TimeoutException:
        log_and_print("⚠️ Base listings container didn't load within 10s - continuing anyway.")

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
def extract_shows_from_page(driver):
    shows_list = []

    show_cards = driver.find_elements(By.CSS_SELECTOR, "div[class^='team-']")
    log_and_print(f"🎭 Found {len(show_cards)} shows.")

    for show in show_cards:
        try:
            title = show.find_element(By.CSS_SELECTOR, "div.wpb_wrapper span a").text.strip()
        except Exception:
            title = None

        try:
            show_website = show.find_element(By.CSS_SELECTOR, "div.wpb_wrapper span a").get_attribute("href")
        except Exception:
            show_website = None

        try:
            image_url = show.find_element(By.CSS_SELECTOR, "img").get_attribute("src").strip()
        except Exception:
            image_url = None

        try:
            writer = show.find_element(By.CSS_SELECTOR, "figcaption .width-90").text
            writer = writer.replace("by", "").strip()
        except Exception:
            writer = None

        shows_list.append({
            "title": title,
            "show_website": show_website,
            "image_url": image_url,
            "writer": writer,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    return shows_list


# ------------------------------------------------------------
# EXTRACT DATES FROM INDIVIDUAL SHOW PAGE
# ------------------------------------------------------------
def extract_show_duration(driver):
    try:
        show_duration = driver.find_element(By.CSS_SELECTOR, "span.heading-1").text.strip()
        return show_duration
    except Exception as e:
        log_and_print(f"❌ Error extracting show duration: {e}")
        return None


# ------------------------------------------------------------
# EXTRACT TICKET INFO FROM INDIVIDUAL SHOW PAGE
# ------------------------------------------------------------
def extract_ticket_info(driver):
    """Exctract price for different age group and ticket types"""
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#accordion-one-link-2 .panel-body"))
        )
        log_and_print(f"ticket info found")
    except TimeoutException:
        log_and_print("⚠️ ticket info not found.")

    ticket_list=[]
    try:
        ticket_price = driver.find_element(By.CSS_SELECTOR, "#accordion-one-link-2 .panel-body p:nth-of-type(1)")
        ticket_price= ticket_price.get_attribute("textContent").strip()
        parts = ticket_price.split()

        if len(parts) >= 12:
            adult_price = parts[0]
            senior_price = parts[2]
            student_price = parts[5]
            service_fee = parts[11]
    except Exception:
        log_and_print(f"❌ Error extracting ticket info")

    ticket_list.append({
            "adult_price": adult_price,
            "senior_price": senior_price,
            "student_price": student_price,
            "service_fee": service_fee,
            "ticket_price": ticket_price,
        })
    return ticket_list


def extract_spektrix_dates(driver):
    date_list=[]
    try:
        # 1. Extract Event ID from the GET TICKETS button
        btn = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "button.ticket-buy-now-btn"))
        )
    except:
        log_and_print("⚠️ No Get Ticket button found")
        return None


    try:
        event_id = btn.get_attribute("data-event-id").strip()
        if not event_id:
            return None

        # 2. Build the Spektrix URL directly
        spektrix_url = f"https://secure.keegantheatre.com/keegantheatre/website/eventdetails.aspx?WebEventID={event_id}&resize=true"
        log_and_print(f"➡️ Loading Spektrix URL: {spektrix_url}")

        driver.get(spektrix_url)
        time.sleep(2)

        # 3. Extract dates
        select_elem = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select.EventDatesList"))
        )

        options = select_elem.find_elements(By.TAG_NAME, "option")
        dates_times = [opt.text.strip() for opt in options if opt.text.strip()]

        date_list= []
        for dt in dates_times:
            dates= dt.split("-")[0].strip()
            times= dt.split("-")[1].strip()

            parsed_date = parser.parse(dates)
            formatted_date = parsed_date.strftime("%Y-%m-%d")
            day_of_week = parsed_date.strftime("%A")

            date_list.append({
                    "date": formatted_date,
                    "day_of_week": day_of_week,
                    "time": times
                })

        log_and_print(f"📅 Extracted {len(date_list)} separate date/time rows")
        return date_list

    except Exception as e:
        log_and_print(f"❌ Spektrix extraction error: {e}")
        return None


# ------------------------------------------------------------
# SAFELY RUN THE DRIVER
# ------------------------------------------------------------
def safe_get(driver, url, retries=3):
    for attempt in range(retries):
        try:
            driver.get(url)
            return True
        except Exception as e:
            print(f"⚠️ Error loading {url}: {e}")
            time.sleep(3)
    print(f"❌ Failed to load after retries: {url}")
    return False


# ------------------------------------------------------------
# MAIN SCRAPER
# ------------------------------------------------------------
def scrape_shows():
    driver = setup_browser()
    all_shows = []
    pages = ["https://keegantheatre.com"]

    try:
        for url in pages:
            log_and_print(f"\n🌍 Scraping page: {url}")
            driver.get(url)
            time.sleep(3)
            scroll_to_load_all_shows(driver)

            shows = extract_shows_from_page(driver)

            # visit each show page
            for item in shows:
                if not item["show_website"]:
                    continue

                if not safe_get(driver, item["show_website"]):
                    item["show_duration"] = None
                    all_shows.append(item)
                    continue

                time.sleep(2)
                item["show_duration"] = extract_show_duration(driver)

                ticket_list = extract_ticket_info(driver)
                if ticket_list:
                    item.update(ticket_list[0])  # merge ticket info into current show

                date_time_list = extract_spektrix_dates(driver)
                if date_time_list:
                    all_shows.extend({**item, **dt} for dt in date_time_list)
                else:
                    all_shows.append(item)

        # Save results
        if all_shows:
            output_file = "keagan3.csv"
            pd.DataFrame(all_shows).to_csv(output_file, index=False)
            log_and_print(f"Saved {len(all_shows)} rows to {output_file}")
        else:
            log_and_print("No shows were found or scraped")

    except Exception as e:
        log_and_print(f"💥 Fatal error: {repr(e)}")
        traceback.print_exc()

    finally:
        try:
            driver.quit()
            log_and_print("✅ Browser closed successfully.")
        except Exception:
            log_and_print("⚠️ Error closing browser.")


# ------------------------------------------------------------
# RUN
# ------------------------------------------------------------
if __name__ == "__main__":
    scrape_shows()
