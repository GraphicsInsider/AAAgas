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
AAA_STATE_URL = "https://gasprices.aaa.com/state-gas-price-averages/"

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDS = os.environ["GOOGLE_CREDS"]

BROWSERLESS_API_KEY = os.environ.get("BROWSERLESS_API_KEY", "")
BROWSERLESS_REGION = os.environ.get("BROWSERLESS_REGION", "production-sfo")

NATIONAL_SHEET_NAME = "AAA Gas Prices"
NATIONAL_HISTORY_SHEET_NAME = "Gas Price History"
STATE_SHEET_NAME = "State Gas Prices"
STATE_HISTORY_SHEET_NAME = "State Gas Price History"


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


def parse_price_value(text: str) -> float:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    if not m:
        raise ValueError(f"Could not parse price from: {text}")
    return float(m.group(1))


def first_index(headers: list[str], needle: str) -> int | None:
    needle = needle.lower()
    for idx, header in enumerate(headers):
        if needle in normalize_key(header):
            return idx
    return None


def find_state_table_and_headers(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        if not headers:
            continue

        keys = [normalize_key(h) for h in headers]
        has_state = any("state" in k for k in keys)
        has_current = any(("current" in k) or ("latest" in k) or ("avg" in k) for k in keys)

        if has_state and has_current:
            return table, headers

    raise ValueError("Could not find the AAA state price table.")


def get_state_prices():
    html = fetch_state_html()
    soup = BeautifulSoup(html, "html.parser")
    table, headers = find_state_table_and_headers(soup)

    header_keys = [normalize_key(h) for h in headers]
    state_idx = first_index(headers, "state")
    current_idx = first_index(headers, "current")
    if current_idx is None:
        current_idx = first_index(headers, "latest")
    if current_idx is None:
        current_idx = first_index(headers, "avg")

    if state_idx is None or current_idx is None:
        raise ValueError(f"Could not find required state/current columns. Headers: {headers}")

    comparison_specs = []
    idx = first_index(headers, "yesterday")
    if idx is not None:
        comparison_specs.append(("yesterday", "Yesterday", idx))
    idx = first_index(headers, "week")
    if idx is not None:
        comparison_specs.append(("week_ago", "Week Ago", idx))
    idx = first_index(headers, "month")
    if idx is not None:
        comparison_specs.append(("month_ago", "Month Ago", idx))
    idx = first_index(headers, "year")
    if idx is not None:
        comparison_specs.append(("year_ago", "Year Ago", idx))

    rows = []
    for row in table.find_all("tr")[1:]:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) <= max([state_idx, current_idx] + [idx for _, _, idx in comparison_specs]):
            continue

        state = cells[state_idx].strip()
        if not state:
            continue

        if normalize_key(state) in {"national", "usaverage", "usa", "unitedstates"}:
            continue

        record = {
            "state": state,
            "current": parse_price_value(cells[current_idx]),
        }

        for key, _, idx in comparison_specs:
            record[key] = parse_price_value(cells[idx])

        rows.append(record)

    if not rows:
        raise ValueError("No state rows parsed from the AAA state table.")

    return rows, comparison_specs


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


def _col_letter(n: int) -> str:
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def write_to_sheet(prices: dict[str, float]) -> None:
    sheet = _get_client().open_by_key(SHEET_ID).worksheet(NATIONAL_SHEET_NAME)
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
    sheet.update("A1", values, value_input_option="USER_ENTERED")
    sheet.format("B2:B5", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})
    sheet.format("B6:B8", {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}})


def write_to_history(prices: dict[str, float]) -> None:
    spreadsheet = _get_client().open_by_key(SHEET_ID)

    history_sheet = _ensure_worksheet(spreadsheet, NATIONAL_HISTORY_SHEET_NAME, rows=1000, cols=5)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    header = ["Date", "Latest", "WeekEarlier", "MonthEarlier", "YearEarlier"]

    existing = history_sheet.get_all_values()
    has_any_data = any(any(str(cell).strip() for cell in row) for row in existing)

    if not has_any_data:
        history_sheet.update("A1", [header], value_input_option="RAW")
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


def write_state_snapshot(state_rows: list[dict], comparison_specs: list[tuple]) -> None:
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    sheet = _ensure_worksheet(spreadsheet, STATE_SHEET_NAME, rows=100, cols=10)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    headers = ["State", "Current"] + [label for _, label, _ in comparison_specs] + ["Date"]
    values = [headers]

    for row in state_rows:
        values.append(
            [row["state"], row["current"]]
            + [row.get(key, "") for key, _, _ in comparison_specs]
            + [today]
        )

    sheet.clear()
    sheet.update("A1", values, value_input_option="USER_ENTERED")

    last_numeric_col = 2 + len(comparison_specs)
    if len(values) > 1:
        sheet.format(
            f"B2:{_col_letter(last_numeric_col)}{len(values)}",
            {"numberFormat": {"type": "NUMBER", "pattern": "$0.000"}},
        )


def write_state_history(state_rows: list[dict], comparison_specs: list[tuple]) -> None:
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    sheet = _ensure_worksheet(spreadsheet, STATE_HISTORY_SHEET_NAME, rows=5000, cols=12)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    existing = sheet.get_all_values()

    if any(row and row[0] == today for row in existing[1:]):
        print(f"State history: rows for {today} already exist, skipping.")
        return

    headers = ["Date", "State", "Current"] + [label for _, label, _ in comparison_specs]
    if not existing:
        sheet.update("A1", [headers], value_input_option="RAW")

    rows = []
    for row in state_rows:
        rows.append(
            [today, row["state"], row["current"]]
            + [row.get(key, "") for key, _, _ in comparison_specs]
        )

    sheet.append_rows(rows, value_input_option="RAW")
    print(f"State history: appended {len(rows)} rows for {today}.")


def main() -> None:
    errors = []

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

    try:
        state_rows, comparison_specs = get_state_prices()
        write_state_snapshot(state_rows, comparison_specs)
        write_state_history(state_rows, comparison_specs)
        print("Updated state gas price sheets.")
        print(f"Parsed {len(state_rows)} state rows.")
    except Exception as e:
        errors.append(f"State update failed: {e}")

    if errors:
        raise RuntimeError(" | ".join(errors))


if __name__ == "__main__":
    main()
