import json
import os
import re
from datetime import datetime, timezone

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

AAA_URL = "https://gasprices.aaa.com/?stream=topGas"

# Put these in GitHub Actions secrets / env vars
SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDS = os.environ["GOOGLE_CREDS"]  # full service-account JSON string


def fetch_aaa_html() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://gasprices.aaa.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    response = requests.get(AAA_URL, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


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
    """
    Find the line that starts with the label and return the first dollar amount
    on that line. On AAA's page, that first amount is the Regular price.
    """
    label_re = re.escape(label)
    pattern = re.compile(rf"^{label_re}\s+\$([0-9]+(?:\.[0-9]+)?)\b")

    for line in lines:
        m = pattern.match(line)
        if m:
            return float(m.group(1))

    # Fallback: scan for a line containing the label anywhere, then grab the first $amount after it.
    for line in lines:
        if label in line:
            m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", line)
            if m:
                return float(m.group(1))

    raise ValueError(f"Could not find price text for label: {label}")


def get_prices() -> dict[str, float]:
    html = fetch_aaa_html()
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

    now_local = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    # Keep the same layout your Apps Script endpoint expects
    values = [
        ["Metric", "Value", "Unit", "Source", "Refreshed"],
        ["Current average", prices["current"], "USD/gal", AAA_URL, now_local],
        ["Week ago average", prices["week_ago"], "USD/gal", AAA_URL, now_local],
        ["Month ago average", prices["month_ago"], "USD/gal", AAA_URL, now_local],
        ["Year ago average", prices["year_ago"], "USD/gal", AAA_URL, now_local],
        ["Difference vs week ago", f"=B2-B3", "", "", ""],
        ["Difference vs month ago", f"=B2-B4", "", "", ""],
        ["Difference vs year ago", f"=B2-B5", "", "", ""],
        ["Last refresh", now_local, "", "", ""],
    ]

    sheet.clear()
    sheet.update("A1:E9", values)

    # Formats
    sheet.format("B2:B5", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})
    sheet.format("B6:B8", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})
    sheet.format("B9", {"numberFormat": {"type": "TEXT"}})


def main() -> None:
    prices = get_prices()
    write_to_sheet(prices)
    print("Updated AAA Gas Prices sheet:")
    print(prices)


if __name__ == "__main__":
    main()
