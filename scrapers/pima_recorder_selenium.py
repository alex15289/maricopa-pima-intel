"""
Pima Recorder Scraper (Selenium, human-in-loop)
================================================

Adapted from client's working scraper for the Tyler Pima Recorder portal.

HOW IT WORKS:
  - Opens Chrome to the search page
  - You manually solve the CAPTCHA + set search criteria (doc type + date range)
  - After you click Search, press Enter in the terminal
  - Scraper walks through all results and extracts metadata
  - Output written to data/Pimacounty_Data.csv
  - Resume-safe: skips records already in the CSV (by Sequence Number)

USAGE:
  python scrapers/pima_recorder_selenium.py
  or (recommended):
  ./scrape-pima.sh

REQUIREMENTS:
  pip install selenium undetected-chromedriver webdriver-manager scrapy openpyxl

NOTES:
  - Run this daily or weekly to get fresh recorder data
  - One session per doc type (pre-foreclosures, lis pendens, etc.) — easier
    to filter in the Tyler UI than to extract all types at once
  - If CAPTCHA reappears mid-scrape, solve it in the browser and scraping
    continues automatically
"""
import csv
import time
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from scrapy import Selector
from webdriver_manager.chrome import ChromeDriverManager

# ────────────────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "Pimacounty_Data.csv"
START_URL = "https://pimacountyaz-web.tylerhost.net/web/search/DOCSEARCH55S6"

FIELDS = [
    "Document Type", "Sequence Number", "Recording Date", "Number of Pages",
    "Grantors", "Grantees", "Parcel ID", "Lot Number", "Address",
    "Legal Description", "Related Document Type",
    "Related Document Recording Date", "Related Document External ID",
    "Related Document Related Text",
]


# ────────────────────────────────────────────────────────────────────────────
# RESUME SUPPORT — skip already-scraped sequence numbers
# ────────────────────────────────────────────────────────────────────────────

def read_existing_sequences(csv_path: Path) -> set:
    """Return the set of Sequence Numbers already in the CSV, for resume support."""
    if not csv_path.exists():
        return set()
    try:
        csv.field_size_limit(5_000_000)
        seen = set()
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                seq = (row.get("Sequence Number") or "").strip()
                if seq:
                    seen.add(seq)
        return seen
    except Exception as e:
        print(f"[warn] couldn't read existing csv: {e}")
        return set()


# ────────────────────────────────────────────────────────────────────────────
# DRIVER INIT
# ────────────────────────────────────────────────────────────────────────────

def initialize_driver():
    """Standard Chrome — Tyler portal doesn't need undetected-chromedriver."""
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service)


# ────────────────────────────────────────────────────────────────────────────
# DATA EXTRACTION
# ────────────────────────────────────────────────────────────────────────────

def extract_main_data(response):
    """Extract all fields from a single document detail view."""

    def one(xpath):
        text = response.xpath(xpath).get()
        return text.strip() if text else None

    def many(xpath):
        data = response.xpath(xpath).getall()
        return [s.replace("\n", "").replace("\xa0", "").strip()
                for s in data if s.strip()]

    document_type   = one("//li[contains(text(), 'Document Type')]/following-sibling::li/text()")
    sequence_number = one("//strong[text()='Sequence Number:']/following::div[1]/text()")
    recording_date  = one("//strong[text()='Recording Date:']/following::div[1]/text()")
    number_of_pages = one("//strong[text()='Number Pages:']/following::div[1]/text()")

    grantors_list = many("//strong[text()='Grantor:']/following::div[1]//text()")
    grantors = "; ".join(grantors_list) if grantors_list else None

    grantees_list = many("//strong[text()='Grantee:']/following::div[1]//text()")
    grantees = "; ".join(grantees_list) if grantees_list else None

    # Parse legal description to extract parcel ID + lot number + address
    legal_parts = many("//li[contains(text(), 'Legal Description')]/following-sibling::li//text()")
    parcel_id = lot_number = address = legal_description = None

    if legal_parts:
        # Extract parcel ID
        parcel_items = [p for p in legal_parts if "Parcel ID" in p]
        if parcel_items:
            try:
                parcel_id = parcel_items[0].split(":", 1)[1].strip()
            except Exception:
                parcel_id = None

        # Extract lot number
        lot_items = [p for p in legal_parts if "Lot" in p]
        if lot_items:
            lot_number = lot_items[0].strip()

        legal_description = " ".join(legal_parts)

        # Best-effort address: legal description minus parcel ID + lot
        addr_text = legal_description
        if parcel_id:
            addr_text = addr_text.replace(parcel_id, "")
        if lot_number:
            addr_text = addr_text.replace(lot_number, "")
        addr_text = addr_text.replace("Parcel ID#:", "").strip()
        address = addr_text or None

    # Related documents
    related_docs = []
    for row in response.xpath("//div[contains(@class, 'ss-row related-table-row')]"):
        related_type   = row.xpath("./div[1]//td[1]/text()").get()
        related_date   = row.xpath("./div[1]//td[2]/text()").get()
        related_extid  = row.xpath("./div[1]//td[3]/a/text()").get()
        related_parts  = [s.replace("\n", "").replace("\xa0", "").strip()
                          for s in row.xpath("./div[2]/div[2]//text()").getall()
                          if s.strip()]
        related_docs.append({
            "documentType": related_type,
            "recordingDate": related_date,
            "externalId": related_extid,
            "relatedText": " ".join(related_parts) if related_parts else None,
        })

    return {
        "Document Type": document_type,
        "Sequence Number": sequence_number,
        "Recording Date": recording_date,
        "Number of Pages": number_of_pages,
        "Grantors": grantors,
        "Grantees": grantees,
        "Parcel ID": parcel_id,
        "Lot Number": lot_number,
        "Address": address,
        "Legal Description": legal_description,
        "_related": related_docs,
    }


def write_row(writer, data):
    """Write one or more rows — one per related doc, or one if no related docs."""
    related = data.pop("_related", [])
    base = data
    if related:
        for rd in related:
            row = dict(base)
            row["Related Document Type"]           = rd.get("documentType")
            row["Related Document Recording Date"] = rd.get("recordingDate")
            row["Related Document External ID"]    = rd.get("externalId")
            row["Related Document Related Text"]   = rd.get("relatedText")
            writer.writerow(row)
    else:
        row = dict(base)
        row["Related Document Type"] = None
        row["Related Document Recording Date"] = None
        row["Related Document External ID"] = None
        row["Related Document Related Text"] = None
        writer.writerow(row)


# ────────────────────────────────────────────────────────────────────────────
# SCRAPING LOOP
# ────────────────────────────────────────────────────────────────────────────

def scrape(driver, writer, seen_sequences):
    """Main scrape loop — walks through result pages, extracts each doc."""
    driver.get(START_URL)

    print()
    print("┌─────────────────────────────────────────────────────────────┐")
    print("│  Chrome is open. Now in the browser:                        │")
    print("│    1. Solve the CAPTCHA                                     │")
    print("│    2. Choose a Document Type (e.g. NOTICE OF TRUSTEE'S SALE)│")
    print("│    3. Set a Recording Date range                            │")
    print("│    4. Click the SEARCH button                               │")
    print("│  Then come back here and press ENTER.                       │")
    print("└─────────────────────────────────────────────────────────────┘")
    input("Press ENTER when search results are showing... ")

    # Wait for results list
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.XPATH, "//li[contains(@id, 'searchRowDOC')]"))
    )
    time.sleep(2)

    # Click the first non-scraped listing to enter detail view
    first_unscraped_index = find_first_unscraped(driver, seen_sequences)
    if first_unscraped_index is None:
        print("[info] no new records to scrape — everything is already in your CSV.")
        return

    open_detail_view(driver, first_unscraped_index)

    # Walk through detail pages using "Next Result" button
    prev_sequence = None
    scraped_count = 0

    while True:
        time.sleep(5)
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH, "//div[@id='documentIndexingInformation']"))
            )
        except Exception:
            print("[warn] detail page didn't load — stopping.")
            break

        response = Selector(text=driver.page_source)
        data = extract_main_data(response)
        current_sequence = data["Sequence Number"]

        # Wait if page hasn't advanced (common: CAPTCHA reappeared)
        retry = 0
        while current_sequence == prev_sequence and retry < 3:
            print(f"[wait] sequence unchanged ({current_sequence}), waiting 40s (retry {retry+1}/3)...")
            time.sleep(40)
            if not click_next_result(driver):
                break
            time.sleep(3)
            response = Selector(text=driver.page_source)
            data = extract_main_data(response)
            current_sequence = data["Sequence Number"]
            retry += 1

        if current_sequence and current_sequence not in seen_sequences:
            print(f"[scrape] {current_sequence} — {data['Document Type']}")
            write_row(writer, data)
            seen_sequences.add(current_sequence)
            scraped_count += 1
        elif current_sequence:
            print(f"[skip] {current_sequence} — already scraped")

        prev_sequence = current_sequence

        if not click_next_result(driver):
            print(f"[done] reached end of results. scraped {scraped_count} new records.")
            break


def find_first_unscraped(driver, seen_sequences):
    """Walk result pages to find the first listing NOT already in the CSV."""
    while True:
        time.sleep(2)
        response = Selector(text=driver.page_source)
        listings = [
            s.replace("\n", "").replace("\t", "").replace("\xa0", "").strip()
            for s in response.xpath("//li[contains(@id, 'searchRowDOC')]//h1//text()").getall()
            if s.strip()
        ]
        for idx, listing in enumerate(listings):
            if listing not in seen_sequences:
                return idx
        # All on this page already scraped — try next page
        try:
            next_btn = driver.find_element(
                By.XPATH, "//a[text()='Next' and not(contains(@class, 'disabled'))]"
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", next_btn)
            time.sleep(1)
            driver.execute_script("arguments[0].click();", next_btn)
            time.sleep(3)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, "//li[contains(@id, 'searchRowDOC')]"))
            )
        except Exception:
            return None


def open_detail_view(driver, index):
    """Click into the result at `index` to enter the detail view."""
    listing = driver.find_element(By.XPATH, f"//li[contains(@id, 'searchRowDOC')][{index + 1}]")
    driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", listing)
    time.sleep(2)
    driver.execute_script("arguments[0].click();", listing)
    time.sleep(2)
    view_btn = driver.find_element(By.XPATH, f"//a[@title='View Document'][{index + 1}]")
    driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", view_btn)
    time.sleep(2)
    driver.execute_script("arguments[0].click();", view_btn)


def click_next_result(driver) -> bool:
    """Click the 'Next Result' button in the detail view. Returns False if disabled/missing."""
    try:
        btn = driver.find_element(
            By.XPATH, "//a[text()='Next Result' and not(contains(@class, 'disabled'))]"
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", btn)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", btn)
        return True
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────────────

def main():
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_exists = CSV_PATH.exists()
    seen = read_existing_sequences(CSV_PATH)
    print(f"[info] {len(seen):,} records already in {CSV_PATH.name} — resume mode.")

    driver = initialize_driver()
    try:
        with open(CSV_PATH, "a" if file_exists else "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if not file_exists:
                writer.writeheader()
            scrape(driver, writer, seen)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
