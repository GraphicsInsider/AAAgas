import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

AAA_URL = "https://gasprices.aaa.com/?stream=topGas"
SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDS = os.environ["GOOGLE_CREDS"]
BROWSERLESS_API_KEY = os.environ.get("BROWSERLESS_API_KEY", "")
BROWSERLESS_REGION = os.environ.get("BROWSERLESS_REGION", "production-sfo")


def fetch_aaa_html() -> str | None:
    if BROWSERLESS_API_KEY:
        browserless_url = f"https://{BROWSERLESS_REGION}.browserless.io/unblock?token={BROWSERLESS_API_KEY}"
        payload = {"url": AAA_URL}

        last_exc = None
        for attempt in range(1, 4):
            try:
                print(f"Browserless attempt {attempt} via /unblock...")
                r = requests.post(browserless_url, json=payload, timeout=60)
                if r.status_code == 403:
                    print("Browserless returned 403 for AAA. Falling back to direct fetch.")
                    break
                r.raise_for_status()

                try:
                    response_json = r.json()
                    if isinstance(response_json, dict) and "content" in response_json:
                        html = response_json["content"]
                    else:
                        html = r.text
                except ValueError:
                    html = r.text

                if html and len(html) > 100:
                    return html

                print(f"Browserless returned short response: {len(html)} bytes")
            except Exception as e:
                last_exc = e
                print(f"Browserless attempt {attempt} failed: {e}")
                time.sleep(3 * attempt)

        if last_exc:
            print(f"Browserless failed after retries: {last_exc}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://gasprices.aaa.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })

    last_exc = None
    for attempt in range(1, 4):
        try:
            r = session.get(AAA_URL, timeout=30, allow_redirects=True)
            if r.status_code == 403:
                print("AAA returned 403 Forbidden. Skipping update this run.")
                return None
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_exc = e
            print(f"Attempt {attempt} failed: {e}")
            time.sleep(3 * attempt)

    print(f"All attempts failed: {last_exc}")
    return None


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def extract_regular_price(lines: list[str], label: str) -> float:
    pattern = re.compile(rf"^{re.escape(label)}\s+\$([0-9]+(?:\.[0-9]+)?)\b")
    for line in lines:
        m = pattern.match(line)
        if m:
            return float(m.group(1))

    for line in lines:
        if label in line:
            m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", line)
            if m:
                return float(m.group(1))

    raise ValueError(f"Could not find price text for label: {label}")


def get_prices() -> dict[str, float] | None:
    html = fetch_aaa_html()
    if not html:
        return None

    lines = html_to_lines(html)
    return {
        "current": extract_regular_price(lines, "Current Avg."),
        "week_ago": extract_regular_price(lines, "Week Ago Avg."),
        "month_ago": extract_regular_price(lines, "Month Ago Avg."),
        "year_ago": extract_regular_price(lines, "Year Ago Avg."),
    }


def write_to_sheet(prices: dict[str, float]) -> None:
    creds_info = json.loads(GOOGLE_CREDS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)

    sheet = client.open_by_key(SHEET_ID).worksheet("AAA Gas Prices")
    now_local = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")

    values = [
        ["Metric", "Value", "Unit", "Source", "Refreshed"],
        ["Current average", prices["current"], "USD/gal", AAA_URL, now_local],
        ["Week ago average", prices["week_ago"], "USD/gal", AAA_URL, now_local],
        ["Month ago average", prices["month_ago"], "USD/gal", AAA_URL, now_local],
        ["Year ago average", prices["year_ago"], "USD/gal", AAA_URL, now_local],
        ["Difference vs week ago", "=B2-B3", "", "", ""],
        ["Difference vs month ago", "=B2-B4", "", "", ""],
        ["Difference vs year ago", "=B2-B5", "", "", ""],
        ["Last refresh", now_local, "", "", ""],
    ]

    sheet.clear()
    sheet.update(values=values, range_name="A1:E9", value_input_option="USER_ENTERED")

    sheet.format("B2:B5", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})
    sheet.format("B6:B8", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})


def main() -> None:
    prices = get_prices()
    if prices is None:
        print("No update written; leaving existing sheet values intact.")
        return

    write_to_sheet(prices)
    print("Updated AAA Gas Prices sheet:")
    print(prices)


if __name__ == "__main__":
    main()
