import json
import os
import re
import time
import csv
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

AAA_URL = "https://gasprices.aaa.com/?stream=topGas"
AAA_STATE_URL = "https://gasprices.aaa.com/state-gas-price-averages/"

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDS = os.environ["GOOGLE_CREDS"]

BROWSERLESS_API_KEY = os.environ.get("BROWSERLESS_API_KEY", "")
BROWSERLESS_REGION = os.environ.get("BROWSERLESS_REGION", "production-sfo")

NATIONAL_SHEET_NAME = "AAA Gas Prices"
NATIONAL_HISTORY_SHEET_NAME = "Gas Price History"
STATE_SHEET_NAME = "State Gas Prices"
STATE_HISTORY_SHEET_NAME = "State Gas Price History"
STATE_WAR_START_SHEET_NAME = "State War Start"

RUN_MODE = os.environ.get("RUN_MODE", "all")


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
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://gasprices.aaa.com/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )

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


def fetch_state_html() -> str:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://gasprices.aaa.com/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )

    r = session.get(AAA_STATE_URL, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r.text


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
    for i, line in enumerate(lines):
        if label.lower() in line.lower():
            for j in range(i + 1, min(i + 10, len(lines))):
                m = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", lines[j])
                if m:
                    return float(m.group(1))

    for line in lines:
        if label.lower() in line.lower():
            m = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", line)
            if m:
                return float(m.group(1))

    raise ValueError(f"Could not find price text for label: {label}")


def get_national_prices() -> dict[str, float] | None:
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


def normalize_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def build_state_keys() -> set[str]:
    pairs = [
        ("Alabama", "AL"),
        ("Alaska", "AK"),
        ("Arizona", "AZ"),
        ("Arkansas", "AR"),
        ("California", "CA"),
        ("Colorado", "CO"),
        ("Connecticut", "CT"),
        ("Delaware", "DE"),
        ("Florida", "FL"),
        ("Georgia", "GA"),
        ("Hawaii", "HI"),
        ("Idaho", "ID"),
        ("Illinois", "IL"),
        ("Indiana", "IN"),
        ("Iowa", "IA"),
        ("Kansas", "KS"),
        ("Kentucky", "KY"),
        ("Louisiana", "LA"),
        ("Maine", "ME"),
        ("Maryland", "MD"),
        ("Massachusetts", "MA"),
        ("Michigan", "MI"),
        ("Minnesota", "MN"),
        ("Mississippi", "MS"),
        ("Missouri", "MO"),
        ("Montana", "MT"),
        ("Nebraska", "NE"),
        ("Nevada", "NV"),
        ("New Hampshire", "NH"),
        ("New Jersey", "NJ"),
        ("New Mexico", "NM"),
        ("New York", "NY"),
        ("North Carolina", "NC"),
        ("North Dakota", "ND"),
        ("Ohio", "OH"),
        ("Oklahoma", "OK"),
        ("Oregon", "OR"),
        ("Pennsylvania", "PA"),
        ("Rhode Island", "RI"),
        ("South Carolina", "SC"),
        ("South Dakota", "SD"),
        ("Tennessee", "TN"),
        ("Texas", "TX"),
        ("Utah", "UT"),
        ("Vermont", "VT"),
        ("Virginia", "VA"),
        ("Washington", "WA"),
        ("West Virginia", "WV"),
        ("Wisconsin", "WI"),
        ("Wyoming", "WY"),
        ("District of Columbia", "DC"),
    ]

    keys = set()
    for name, abbr in pairs:
        keys.add(normalize_key(name))
        keys.add(normalize_key(abbr))

    keys.update({"districtofcolumbia", "washingtondc", "dc"})
    return keys


STATE_KEYS = build_state_keys()


def is_state_label(text: str) -> bool:
    return normalize_key(text) in STATE_KEYS


def parse_price_value(text: str) -> float | None:
    cleaned = text.replace(",", " ").strip()
    m = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", cleaned)
    if not m:
        return None
    return float(m.group(1))


def get_state_prices() -> list[dict[str, object]]:
    html = fetch_state_html()
    soup = BeautifulSoup(html, "html.parser")

    rows: list[dict[str, object]] = []

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if len(cells) < 2:
            continue

        state = cells[0].strip()
        if not state or not is_state_label(state):
            continue

        regular = None
        for cell in cells[1:]:
            regular = parse_price_value(cell)
            if regular is not None:
                break

        if regular is None:
            continue

        rows.append({"state": state, "regular": regular})

    if not rows:
        raise ValueError("Could not parse any AAA state rows.")

    dedup: dict[str, dict[str, object]] = {}
    for row in rows:
        key = normalize_key(str(row["state"]))
        if key not in dedup:
            dedup[key] = row

    result = list(dedup.values())
    result.sort(key=lambda r: str(r["state"]))
    return result


def _get_client() -> gspread.Client:
    creds_info = json.loads(GOOGLE_CREDS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)


def _ensure_worksheet(spreadsheet: gspread.Spreadsheet, title: str, rows: int, cols: int):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def write_to_sheet(prices: dict[str, float]) -> None:
    sheet = _get_client().open_by_key(SHEET_ID).worksheet(NATIONAL_SHEET_NAME)
    now_local = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")

    values = [
        ["Metric", "Value", "Unit", "Source", "Refreshed"],
        ["Current average", prices["current"], "USD/gal", AAA_URL, now_local],
        ["Week ago average", prices["week_ago"], "USD/gal", AAA_URL, now_local],
        ["Month ago average", prices["month_ago"], "USD/gal", AAA_URL, now_local],
        ["Year ago average", prices["year_ago"], "USD/gal", AAA_URL, now_local],
        ["Iran war start (Feb. 28)", 2.982, "USD/gal", "Manual", now_local],
        ["Difference vs week ago", "=B2-B3", "", "", ""],
        ["Difference vs month ago", "=B2-B4", "", "", ""],
        ["Difference vs year ago", "=B2-B5", "", "", ""],
        ["Difference vs pre-war", "=B2-B6", "", "", ""],
        ["Last refresh", now_local, "", "", ""],
    ]

    sheet.clear()
    sheet.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
    sheet.format("B2:B6", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})
    sheet.format("B7:B10", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})


def write_to_history(prices: dict[str, float]) -> None:
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    history_sheet = _ensure_worksheet(spreadsheet, NATIONAL_HISTORY_SHEET_NAME, rows=1000, cols=5)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    header = ["Date", "Latest", "WeekEarlier", "MonthEarlier", "YearEarlier"]

    existing = history_sheet.get_all_values()
    has_any_data = any(any(str(cell).strip() for cell in row) for row in existing)

    if not has_any_data:
        history_sheet.update(values=[header], range_name="A1", value_input_option="RAW")
        existing = [header]

    last_date = None
    for row in reversed(existing):
        if len(row) > 0 and str(row[0]).strip() and row[0] != "Date":
            last_date = str(row[0]).strip()
            break

    if last_date == today:
        print(f"History: row for {today} already exists, skipping.")
        return

    history_sheet.append_row(
        [today, prices["current"], prices["week_ago"], prices["month_ago"], prices["year_ago"]],
        value_input_option="RAW",
    )
    print(f"History: appended row for {today}.")


def write_state_snapshot(state_rows: list[dict[str, object]]) -> None:
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    sheet = _ensure_worksheet(spreadsheet, STATE_SHEET_NAME, rows=100, cols=4)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    values = [["State", "Regular", "Date"]]
    for row in state_rows:
        values.append([row["state"], row["regular"], today])

    sheet.clear()
    sheet.update(values=values, range_name="A1", value_input_option="USER_ENTERED")


def write_state_history(state_rows: list[dict[str, object]]) -> None:
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    sheet = _ensure_worksheet(spreadsheet, STATE_HISTORY_SHEET_NAME, rows=5000, cols=4)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    header = ["Date", "State", "Regular"]

    existing = sheet.get_all_values()
    if not existing:
        sheet.update(values=[header], range_name="A1", value_input_option="RAW")
        existing = [header]

    if any(row and str(row[0]).strip() == today for row in existing[1:]):
        print(f"State history: rows for {today} already exist, skipping.")
        return

    rows = []
    for row in state_rows:
        rows.append([today, row["state"], row["regular"]])

    sheet.append_rows(rows, value_input_option="RAW")
    print(f"State history: appended {len(rows)} rows for {today}.")


def write_state_csv() -> None:
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    sheet = spreadsheet.worksheet(STATE_WAR_START_SHEET_NAME)

    rows = sheet.get_all_values()
    if not rows:
        raise ValueError(f"Worksheet '{STATE_WAR_START_SHEET_NAME}' is empty.")

    out_path = Path("data") / "state_war_start.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"Wrote CSV from worksheet '{STATE_WAR_START_SHEET_NAME}': {out_path}")


def main() -> None:
    errors = []

    if RUN_MODE in ("all", "national"):
        try:
            prices = get_national_prices()
            if prices is None:
                print("No national update written; leaving existing national values intact.")
            else:
                write_to_sheet(prices)
                write_to_history(prices)
                print("Updated national AAA Gas Prices sheet.")
                print(prices)
        except Exception as e:
            errors.append(f"National update failed: {e}")

    if RUN_MODE in ("all", "state"):
        try:
            state_rows = get_state_prices()
            write_state_snapshot(state_rows)
            write_state_history(state_rows)
            write_state_csv()
            print("Updated state gas price sheets.")
            print(f"Parsed {len(state_rows)} state rows.")
        except Exception as e:
            errors.append(f"State update failed: {e}")

    if errors:
        raise RuntimeError(" | ".join(errors))


if __name__ == "__main__":
    main()
