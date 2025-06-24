import streamlit as st
import pandas as pd
import requests
import time

st.set_page_config(page_title="Hero or Zero Dashboard", layout="wide")
st.title("🚀 Hero or Zero Trade Dashboard")
st.caption("Live OI + Pivot Levels + Hero/Zero Trade (with 15% Target & 5% SL)")

indices = {
    "NIFTY": ("NIFTY", 50),
    "BANKNIFTY": ("BANKNIFTY", 100),
    "FINNIFTY": ("FINNIFTY", 50),
    "MIDCPNIFTY": ("MIDCPNIFTY", 50)
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_option_chain(symbol, retries=3, delay=3):
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    session = requests.Session()
    for attempt in range(retries):
        try:
            session.get("https://www.nseindia.com", headers=headers, timeout=5)
            response = session.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return {"error": str(e)}

def calculate_levels_and_trade(symbol, data, step):
    if "error" in data:
        return {
            "Index": symbol,
            "Spot": "—",
            "Hero/Zero Trade": "❌ Error",
            "Entry": "—",
            "Target (15%)": "—",
            "Stoploss (5%)": "—",
            "Error": data["error"]
        }

    records = data.get("records", {}).get("data", [])
    spot = data.get("records", {}).get("underlyingValue", None)

    if spot is None:
        return {
            "Index": symbol,
            "Spot": "—",
            "Hero/Zero Trade": "❌ No Spot",
            "Entry": "—",
            "Target (15%)": "—",
            "Stoploss (5%)": "—",
        }

    ce_oi = {r["strikePrice"]: r["CE"]["openInterest"] for r in records if "CE" in r}
    pe_oi = {r["strikePrice"]: r["PE"]["openInterest"] for r in records if "PE" in r}
    max_ce = max(ce_oi.items(), key=lambda x: x[1]) if ce_oi else (0, 0)
    max_pe = max(pe_oi.items(), key=lambda x: x[1]) if pe_oi else (0, 0)

    close = spot
    high = spot + step
    low = spot - step

    pivot = (high + low + close) / 3
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = high + 2 * (pivot - low)
    s3 = low - 2 * (high - pivot)

    atm = round(spot / step) * step
    trade_type = "⚠️ Wait for Breakout"
    entry_price = None
    target_price = None
    stop_loss = None

    for r in records:
        if "CE" in r and "PE" in r and r["strikePrice"] == atm:
            if atm < max_pe[0] and atm < max_ce[0]:
                strike = atm + step
                trade_type = f"📈 BUY {symbol} CE {strike}"
                entry_price = next((x["CE"]["lastPrice"] for x in records if x.get("strikePrice") == strike and "CE" in x), None)
            elif atm > max_pe[0] and atm > max_ce[0]:
                strike = atm - step
                trade_type = f"📉 BUY {symbol} PE {strike}"
                entry_price = next((x["PE"]["lastPrice"] for x in records if x.get("strikePrice") == strike and "PE" in x), None)
            break

    if entry_price is not None and isinstance(entry_price, (int, float)) and entry_price >= 1:
        target_price = round(entry_price * 1.15, 2)
        stop_loss = round(entry_price * 0.95, 2)
        entry_price = round(entry_price, 2)

    return {
        "Index": symbol,
        "Spot": round(spot, 2),
        "Pivot": round(pivot, 2),
        "S1": round(s1, 2),
        "S2": round(s2, 2),
        "S3": round(s3, 2),
        "R1": round(r1, 2),
        "R2": round(r2, 2),
        "R3": round(r3, 2),
        "Hero/Zero Trade": trade_type,
        "Entry": entry_price if entry_price is not None and isinstance(entry_price, (int, float)) else "—",
        "Target (15%)": target_price if target_price is not None else "—",
        "Stoploss (5%)": stop_loss if stop_loss is not None else "—"
    }

results = []
for symbol, (symbol_name, step) in indices.items():
    try:
        data = fetch_option_chain(symbol_name)
        results.append(calculate_levels_and_trade(symbol_name, data, step))
    except Exception as e:
        results.append({
            "Index": symbol_name,
            "Hero/Zero Trade": "❌ Error",
            "Entry": "—",
            "Target (15%)": "—",
            "Stoploss (5%)": "—",
            "Error": str(e)
        })

df = pd.DataFrame(results)
st.dataframe(df, use_container_width=True)
