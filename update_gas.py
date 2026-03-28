import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# --- Fetch AAA page ---
url = "https://gasprices.aaa.com/"
headers = {
    "User-Agent": "Mozilla/5.0"
}

res = requests.get(url, headers=headers)
soup = BeautifulSoup(res.text, "html.parser")

text = soup.get_text(" ", strip=True)

def extract(label):
    import re
    match = re.search(label + r".{0,200}?\$([0-9]+\.[0-9]+)", text)
    return float(match.group(1))

current = extract("Current Avg")
week = extract("Week Ago Avg")
month = extract("Month Ago Avg")
year = extract("Year Ago Avg")

# --- Connect to Google Sheets ---
scopes = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("creds.json", scopes=scopes)
client = gspread.authorize(creds)

sheet = client.open_by_key("YOUR_SHEET_ID").sheet1

# --- Write values ---
sheet.update("B2", current)
sheet.update("B3", week)
sheet.update("B4", month)
sheet.update("B5", year)
sheet.update("B10", datetime.now().strftime("%Y-%m-%d %H:%M"))
