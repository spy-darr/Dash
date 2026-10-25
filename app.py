import streamlit as st
import pandas as pd
import requests
import time
import math
import pytz
import yfinance as yf
from datetime import datetime

st.set_page_config(page_title="Hero or Zero Dashboard", layout="wide")
st.title("🚀 Hero or Zero Trade Dashboard")
st.caption("Index options (Hero/Zero) and intraday stocks — market-based Target / SL with live exit signals")

ist = pytz.timezone("Asia/Kolkata")

indices = {
    "NIFTY": ("NIFTY", 50),
    "BANKNIFTY": ("BANKNIFTY", 100),
    "FINNIFTY": ("FINNIFTY", 50),
    "MIDCPNIFTY": ("MIDCPNIFTY", 50),
    "SENSEX": ("SENSEX", 100)
}

# Indices whose options trade on BSE (symbol -> BSE scrip code); everything else comes from NSE
bse_scrip_codes = {"SENSEX": 1}

# Yahoo Finance tickers used to get the previous day's real High/Low/Close for pivots
yf_tickers = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
    "SENSEX": "^BSESN"
}

RISK_FREE = 0.07
FALLBACK_TARGET_PCT = 0.15   # used only when a market-based plan can't be built
FALLBACK_SL_PCT = 0.05
MAX_RISK_PCT = 0.35          # never risk more than this fraction of premium, even if support is far
SQUARE_OFF_TIME = (15, 15)
EXPIRY_DAY_EXIT_TIME = (14, 30)
REFRESH_SECONDS = 60
MARKET_OPEN_TIME = (9, 15)
OPTION_TIME_STOP_MINUTES = 45   # option trade with no progress after this long is cut
EXIT_ALERT_MINUTES = 10         # how long an "exit now" banner stays up after an exit

NSE_BASE = "https://www.nseindia.com"
# NSE rejects bare user-agents (403), so send full browser-like headers
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{NSE_BASE}/option-chain"
}

@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def fetch_option_chain(symbol, retries=3, delay=3):
    # The old /api/option-chain-indices endpoint was retired (404). The v3 endpoint needs an expiry,
    # which comes from option-chain-contract-info.
    session = requests.Session()
    for attempt in range(retries):
        try:
            session.get(f"{NSE_BASE}/option-chain", headers=NSE_HEADERS, timeout=10)
            info = session.get(f"{NSE_BASE}/api/option-chain-contract-info?symbol={symbol}",
                               headers=NSE_HEADERS, timeout=10)
            info.raise_for_status()
            expiry = info.json()["expiryDates"][0]
            response = session.get(f"{NSE_BASE}/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={expiry}",
                                   headers=NSE_HEADERS, timeout=10)
            response.raise_for_status()
            data = response.json()
            if not data.get("records", {}).get("data"):
                raise ValueError("NSE returned an empty option chain")
            data["records"]["expiryDates"] = [expiry]
            return data
        except (requests.exceptions.RequestException, ValueError, KeyError, IndexError) as e:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return {"error": str(e)}

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_HEADERS = {
    **NSE_HEADERS,
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty"
}

def bse_number(value):
    try:
        return float(str(value).replace(",", "")) if value not in (None, "") else 0
    except ValueError:
        return 0

def bse_leg(row, prefix):
    return {
        "openInterest": bse_number(row.get(f"{prefix}Open_Interest")),
        "lastPrice": bse_number(row.get(f"{prefix}Last_Trd_Price")),
        "impliedVolatility": bse_number(row.get(f"{prefix}IV"))
    }

@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def fetch_bse_option_chain(symbol, retries=3, delay=3):
    """BSE option chain converted to the same shape as NSE's, so the rest of the app is exchange-agnostic."""
    scrip_cd = bse_scrip_codes[symbol]
    session = requests.Session()
    for attempt in range(retries):
        try:
            # BSE's API returns 403 unless the session first picks up cookies from the main site
            session.get("https://www.bseindia.com/", headers={"User-Agent": BSE_HEADERS["User-Agent"]}, timeout=10)
            info = session.get(f"{BSE_API}/ddlExpiry_IV/w", params={"ProductType": "IO", "scrip_cd": scrip_cd},
                               headers=BSE_HEADERS, timeout=10)
            info.raise_for_status()
            bse_expiry = info.json()["Table1"][0]["ExpiryDate"]          # e.g. "29 Oct 2026"
            response = session.get(f"{BSE_API}/DerivOptionChain_IV/w",
                                   params={"Expiry": bse_expiry, "scrip_cd": scrip_cd, "strprice": 0},
                                   headers=BSE_HEADERS, timeout=10)
            response.raise_for_status()
            table = response.json().get("Table") or []
            if not table:
                raise ValueError("BSE returned an empty option chain")

            expiry = bse_expiry.replace(" ", "-")                        # NSE style "29-Oct-2026"
            data = [{
                "strikePrice": int(bse_number(row.get("Strike_Price1"))),
                "expiryDate": expiry,
                "CE": bse_leg(row, "C_"),
                "PE": bse_leg(row, "")
            } for row in table]
            return {"records": {"underlyingValue": bse_number(table[0].get("UlaValue")) or None,
                                "expiryDates": [expiry], "data": data}}
        except (requests.exceptions.RequestException, ValueError, KeyError, IndexError) as e:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return {"error": str(e)}

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_prev_day_ohlc(symbol):
    try:
        hist = yf.Ticker(yf_tickers[symbol]).history(period="10d", interval="1d")
        if hist.empty:
            return None
        # Drop today's (incomplete) candle so pivots use the last completed session
        if hist.index[-1].date() >= datetime.now(ist).date():
            hist = hist.iloc[:-1]
        if hist.empty:
            return None
        last = hist.iloc[-1]
        return {"high": float(last["High"]), "low": float(last["Low"]), "close": float(last["Close"])}
    except Exception:
        return None

# ---------- Option pricing (Black-Scholes) ----------

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_price(spot, strike, t, iv, kind):
    if t <= 0 or iv <= 0:
        return max(spot - strike, 0) if kind == "CE" else max(strike - spot, 0)
    d1 = (math.log(spot / strike) + (RISK_FREE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "CE":
        return spot * norm_cdf(d1) - strike * math.exp(-RISK_FREE * t) * norm_cdf(d2)
    return strike * math.exp(-RISK_FREE * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)

def years_to_expiry(expiry, now):
    expiry_dt = ist.localize(datetime.strptime(expiry, "%d-%b-%Y").replace(hour=15, minute=30))
    seconds = (expiry_dt - now).total_seconds()
    return max(seconds, 15 * 60) / (365 * 24 * 3600)

# ---------- Levels and exit planning ----------

def pivot_levels(symbol, spot, step):
    ohlc = fetch_prev_day_ohlc(symbol)
    if ohlc:
        high, low, close, source = ohlc["high"], ohlc["low"], ohlc["close"], "Prev day"
    else:
        high, low, close, source = spot + step, spot - step, spot, "Approx (no OHLC)"

    return {**pivots_from_ohlc(high, low, close), "Source": source}

def pivots_from_ohlc(high, low, close):
    pivot = (high + low + close) / 3
    return {
        "Pivot": pivot,
        "R1": 2 * pivot - low,
        "S1": 2 * pivot - high,
        "R2": pivot + (high - low),
        "S2": pivot - (high - low),
        "R3": high + 2 * (pivot - low),
        "S3": low - 2 * (high - pivot)
    }

def top_strikes(oi, n=3, min_share=0.5):
    """Strikes with the heaviest OI, ignoring any below min_share of the biggest wall."""
    if not oi:
        return []
    biggest = max(oi.values())
    ranked = sorted(oi.items(), key=lambda x: x[1], reverse=True)[:n]
    return [k for k, v in ranked if v >= biggest * min_share]

def underlying_exit_levels(kind, spot, step, levels, ce_oi, pe_oi):
    """Target/stop on the index itself: nearest real resistance/support from pivots + OI walls."""
    buffer = step * 0.25
    pivots = [levels[k] for k in ("Pivot", "R1", "R2", "R3", "S1", "S2", "S3")]
    resistances = sorted(l for l in pivots + top_strikes(ce_oi) if l > spot + buffer)
    supports = sorted((l for l in pivots + top_strikes(pe_oi) if l < spot - buffer), reverse=True)
    resistance = resistances[0] if resistances else spot + step
    support = supports[0] if supports else spot - step
    return (resistance, support) if kind == "CE" else (support, resistance)

def plan_exit(kind, strike, ltp, iv, t, spot, target_spot, stop_spot):
    """Translate index target/stop into option premiums, anchored to the live LTP."""
    if iv > 0:
        model_now = bs_price(spot, strike, t, iv, kind)
        target = ltp + bs_price(target_spot, strike, t, iv, kind) - model_now
        stop = max(ltp + bs_price(stop_spot, strike, t, iv, kind) - model_now, ltp * (1 - MAX_RISK_PCT))
        if target > ltp > stop:
            return round(target, 2), round(stop, 2), "Market"
    return round(ltp * (1 + FALLBACK_TARGET_PCT), 2), round(ltp * (1 - FALLBACK_SL_PCT), 2), "Fallback %"

def evaluate_exit(trade, ltp, market, now):
    """Decide hold/exit for an open trade on every refresh. Mutates trade (peak, trailing SL, status)."""
    spot, ce_oi, pe_oi = market["spot"], market["ce_oi"], market["pe_oi"]
    entry, target = trade["entry"], trade["target"]
    trade["peak"] = max(trade["peak"], ltp)

    # Trailing stop: breakeven after 50% of the move, then lock half the open profit after 80%
    progress = (trade["peak"] - entry) / (target - entry)
    if progress >= 0.8:
        trade["trail_sl"] = max(trade["trail_sl"], round(entry + (trade["peak"] - entry) * 0.5, 2))
    elif progress >= 0.5:
        trade["trail_sl"] = max(trade["trail_sl"], entry)

    is_ce = trade["kind"] == "CE"
    spot_hit_target = spot >= trade["target_spot"] if is_ce else spot <= trade["target_spot"]
    spot_hit_stop = spot <= trade["stop_spot"] if is_ce else spot >= trade["stop_spot"]
    expiry_day = datetime.strptime(trade["expiry"], "%d-%b-%Y").date() == now.date()

    if ltp >= target:
        return "🎯 BOOK PROFIT", f"Premium hit target {target}"
    if spot_hit_target:
        return "🎯 BOOK PROFIT", f"Spot reached {'resistance' if is_ce else 'support'} {round(trade['target_spot'], 2)}"
    if ltp <= trade["trail_sl"]:
        if trade["trail_sl"] > trade["sl"]:
            return "🔒 EXIT (trailing SL)", f"Gave back profit, trailing SL {trade['trail_sl']}"
        return "🛑 EXIT (stoploss)", f"Premium hit SL {trade['sl']}"
    if spot_hit_stop:
        return "🛑 EXIT (level broken)", f"Spot broke {'support' if is_ce else 'resistance'} {round(trade['stop_spot'], 2)}"
    if (now.hour, now.minute) >= SQUARE_OFF_TIME:
        return "⏰ EXIT (square off)", "Intraday square-off time"
    if expiry_day and (now.hour, now.minute) >= EXPIRY_DAY_EXIT_TIME and progress < 0.5:
        return "⏰ EXIT (theta decay)", "Expiry afternoon and trade not working"

    # Failing trade: exit early instead of waiting for the full stoploss
    in_loss = ltp < entry
    minutes_in = (now - datetime.fromisoformat(trade["opened_ts"])).total_seconds() / 60
    spot_move = (spot - trade["entry_spot"]) if is_ce else (trade["entry_spot"] - spot)
    stop_distance = abs(trade["entry_spot"] - trade["stop_spot"])
    signal = market["signal"]
    if signal and signal[0] != trade["kind"]:
        return "🚨 EXIT (signal reversed)", f"OI setup now favours {signal[0]} — the {trade['kind']} view is invalid"
    if in_loss and stop_distance and spot_move <= -0.5 * stop_distance:
        return "🚨 EXIT (trade failing)", f"Spot moved {round(-spot_move, 1)} pts against, halfway to stop — cut early"
    if in_loss and minutes_in >= OPTION_TIME_STOP_MINUTES and progress < 0.1:
        return "🚨 EXIT (not working)", f"{int(minutes_in)} min in and still below entry — theta is eating the premium"

    # OI shift: the wall that supports the trade is moving against it
    max_ce = max(ce_oi.items(), key=lambda x: x[1])[0] if ce_oi else None
    max_pe = max(pe_oi.items(), key=lambda x: x[1])[0] if pe_oi else None
    oi_against = ((max_ce and max_ce < trade["max_ce"]) or (max_pe and max_pe < trade["max_pe"])) if is_ce else \
                 ((max_ce and max_ce > trade["max_ce"]) or (max_pe and max_pe > trade["max_pe"]))
    if oi_against and in_loss:
        return "🚨 EXIT (OI against)", f"OI walls moved against the trade (CE {max_ce}, PE {max_pe}) while in loss"
    if oi_against:
        return "⚠️ HOLD (OI turning)", f"OI walls moved against the trade (CE {max_ce}, PE {max_pe}) — consider booking"
    if in_loss:
        return "⚠️ HOLD (under water)", f"Below entry — SL {trade['trail_sl']}, fail-exit if spot nears {round(trade['stop_spot'], 2)}"
    return "✅ HOLD", f"Target {target} / SL {trade['trail_sl']}"

def calculate_levels_and_trade(symbol, data, step):
    if "error" in data:
        return {"Index": symbol, "Spot": None, "Error": data["error"]}, None

    records_all = data.get("records", {})
    spot = records_all.get("underlyingValue", None)
    if spot is None:
        return {"Index": symbol, "Spot": None, "Error": "No Spot"}, None

    # Use only the nearest expiry so OI walls, prices and IV all refer to the same contract
    expiry = (records_all.get("expiryDates") or [None])[0]
    # v3 records carry the expiry as "expiryDates"; the old API used "expiryDate"
    records = [r for r in records_all.get("data", [])
               if expiry is None or r.get("expiryDate", r.get("expiryDates")) in (None, expiry)]

    ce_oi = {r["strikePrice"]: r["CE"]["openInterest"] for r in records if "CE" in r}
    pe_oi = {r["strikePrice"]: r["PE"]["openInterest"] for r in records if "PE" in r}
    max_ce = max(ce_oi.items(), key=lambda x: x[1]) if ce_oi else (0, 0)
    max_pe = max(pe_oi.items(), key=lambda x: x[1]) if pe_oi else (0, 0)

    levels = pivot_levels(symbol, spot, step)
    atm = round(spot / step) * step

    signal = None
    for r in records:
        if "CE" in r and "PE" in r and r["strikePrice"] == atm:
            if atm < max_pe[0] and atm < max_ce[0]:
                signal = ("CE", atm + step)
            elif atm > max_pe[0] and atm > max_ce[0]:
                signal = ("PE", atm - step)
            break

    level_row = {
        "Index": symbol,
        "Spot": round(spot, 2),
        "Expiry": expiry or "—",
        **{k: round(levels[k], 2) for k in ("Pivot", "S1", "S2", "S3", "R1", "R2", "R3")},
        "Max CE OI": max_ce[0],
        "Max PE OI": max_pe[0],
        "Pivot Source": levels["Source"]
    }
    market = {
        "spot": spot, "step": step, "expiry": expiry, "records": records,
        "ce_oi": ce_oi, "pe_oi": pe_oi, "levels": levels,
        "max_ce": max_ce[0], "max_pe": max_pe[0], "signal": signal
    }
    return level_row, market

def option_quote(records, strike, kind):
    leg = next((x[kind] for x in records if x.get("strikePrice") == strike and kind in x), None)
    if not leg:
        return None, 0
    return leg.get("lastPrice"), (leg.get("impliedVolatility") or 0) / 100

def open_trade(symbol, market, now):
    kind, strike = market["signal"]
    ltp, iv = option_quote(market["records"], strike, kind)
    if iv <= 0:
        # Illiquid strikes often report IV 0 (common on BSE); use the ATM option's IV instead
        atm = round(market["spot"] / market["step"]) * market["step"]
        iv = option_quote(market["records"], atm, kind)[1]
    if not isinstance(ltp, (int, float)) or ltp < 1 or not market["expiry"]:
        return None

    spot = market["spot"]
    target_spot, stop_spot = underlying_exit_levels(kind, spot, market["step"], market["levels"],
                                                    market["ce_oi"], market["pe_oi"])
    t = years_to_expiry(market["expiry"], now)
    target, sl, basis = plan_exit(kind, strike, ltp, iv, t, spot, target_spot, stop_spot)
    return {
        "kind": kind, "strike": strike, "expiry": market["expiry"],
        "entry": round(ltp, 2), "entry_spot": spot, "opened_at": now.strftime("%H:%M"), "opened_ts": now.isoformat(),
        "target": target, "sl": sl, "trail_sl": sl, "basis": basis,
        "target_spot": target_spot, "stop_spot": stop_spot, "peak": ltp,
        "max_ce": market["max_ce"], "max_pe": market["max_pe"],
        "status": "OPEN", "exit_price": None, "reason": ""
    }

def trade_row(symbol, trade, ltp):
    if trade is None:
        return {"Index": symbol, "Hero/Zero Trade": "⚠️ Wait for Breakout"}
    price = trade["exit_price"] if trade["status"] != "OPEN" else ltp
    pnl = round((price - trade["entry"]) / trade["entry"] * 100, 1) if isinstance(price, (int, float)) else None
    risk = trade["entry"] - trade["sl"]
    return {
        "Index": symbol,
        "Hero/Zero Trade": f"{'📈' if trade['kind'] == 'CE' else '📉'} BUY {symbol} {trade['kind']} {trade['strike']}",
        "Entry": trade["entry"],
        "Entry Time": trade["opened_at"],
        "LTP": price,
        "P&L %": pnl,
        "Target": trade["target"],
        "Stoploss": trade["trail_sl"],
        "R:R": round((trade["target"] - trade["entry"]) / risk, 2) if risk > 0 else None,
        "Spot Target": round(trade["target_spot"], 2),
        "Spot Stop": round(trade["stop_spot"], 2),
        "Plan": trade["basis"],
        "Exit Signal": trade["status"],
        "Reason": trade["reason"]
    }

# ---------- Intraday stocks ----------

NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV",
    "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH",
    "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO"
]

OPENING_RANGE_BARS = 3          # 3 x 5-min candles = first 15 minutes
MIN_RVOL = 1.2                  # today's volume vs the usual volume by this time of day
STOCK_MAX_RISK_PCT = 0.015      # never risk more than 1.5% of price on a stock trade
STOCK_MIN_GAP_PCT = 0.003       # ignore levels closer than 0.3% to price
STOCK_TIME_STOP_MINUTES = 60
MAX_STOCK_TRADES = 5            # only track the strongest few signals at a time

@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def fetch_stock_candles(symbols):
    data = yf.download([f"{s}.NS" for s in symbols], period="5d", interval="5m", group_by="ticker",
                       progress=False, auto_adjust=False, threads=True)
    return {s: data[f"{s}.NS"].dropna(subset=["Close"]) for s in symbols
            if f"{s}.NS" in data.columns.get_level_values(0)}

def analyse_stock(symbol, bars):
    """Prev-day pivots + today's VWAP, opening range and relative volume from 5-min candles."""
    if bars is None or bars.empty:
        return None
    dates = sorted(set(bars.index.date))
    if len(dates) < 2:
        return None
    today_bars = bars[bars.index.date == dates[-1]]
    prev = bars[bars.index.date == dates[-2]]

    pdh, pdl, pdc = float(prev["High"].max()), float(prev["Low"].min()), float(prev["Close"].iloc[-1])
    ltp = float(today_bars["Close"].iloc[-1])
    volume = today_bars["Volume"]
    typical = (today_bars["High"] + today_bars["Low"] + today_bars["Close"]) / 3
    vwap = float((typical * volume).sum() / volume.sum()) if volume.sum() > 0 else ltp
    orh = float(today_bars["High"].iloc[:OPENING_RANGE_BARS].max())
    orl = float(today_bars["Low"].iloc[:OPENING_RANGE_BARS].min())

    bars_so_far = len(today_bars)
    usual = [bars[bars.index.date == d]["Volume"].iloc[:bars_so_far].sum() for d in dates[:-1]]
    usual = sum(usual) / len(usual) if usual else 0
    rvol = float(volume.sum() / usual) if usual else 0

    levels = pivots_from_ohlc(pdh, pdl, pdc)
    signal = None
    if bars_so_far > OPENING_RANGE_BARS and rvol >= MIN_RVOL:
        if ltp > orh and ltp > vwap and ltp > levels["Pivot"]:
            signal = "LONG"
        elif ltp < orl and ltp < vwap and ltp < levels["Pivot"]:
            signal = "SHORT"

    return {
        "symbol": symbol, "ltp": ltp, "change_pct": (ltp / pdc - 1) * 100, "vwap": vwap,
        "orh": orh, "orl": orl, "pdh": pdh, "pdl": pdl, "rvol": rvol, "levels": levels, "signal": signal,
        # strength = how far price has pushed away from VWAP, weighted by participation
        "strength": abs(ltp - vwap) / vwap * 100 * rvol
    }

def plan_stock_trade(a, side, now):
    """Target = next real level with at least 1:1 reward; stop = nearest real level behind price."""
    ltp, gap = a["ltp"], a["ltp"] * STOCK_MIN_GAP_PCT
    pool = [a["levels"][k] for k in ("Pivot", "R1", "R2", "R3", "S1", "S2", "S3")] + \
           [a["pdh"], a["pdl"], a["vwap"], a["orh"], a["orl"]]
    above = sorted(l for l in pool if l > ltp + gap)
    below = sorted((l for l in pool if l < ltp - gap), reverse=True)
    max_risk = ltp * STOCK_MAX_RISK_PCT

    if side == "LONG":
        stop = max(below[0] if below else ltp - max_risk, ltp - max_risk)
        risk = ltp - stop
        target = next((l for l in above if l - ltp >= risk), ltp + 2 * risk)
    else:
        stop = min(above[0] if above else ltp + max_risk, ltp + max_risk)
        risk = stop - ltp
        target = next((l for l in below if ltp - l >= risk), ltp - 2 * risk)

    return {
        "side": side, "entry": round(ltp, 2), "target": round(target, 2), "sl": round(stop, 2),
        "trail_sl": round(stop, 2), "best": 0.0, "orh": a["orh"], "orl": a["orl"],
        "opened_at": now.strftime("%H:%M"), "opened_ts": now.isoformat(),
        "status": "OPEN", "exit_price": None, "reason": ""
    }

def evaluate_stock_exit(trade, a, now):
    is_long = trade["side"] == "LONG"
    ltp, entry, target = a["ltp"], trade["entry"], trade["target"]
    profit = (ltp - entry) if is_long else (entry - ltp)
    trade["best"] = max(trade["best"], profit)

    # Same trailing scheme as options: breakeven at 50% of the move, lock half the profit after 80%
    progress = trade["best"] / abs(target - entry)
    lock = trade["best"] * 0.5 if progress >= 0.8 else 0 if progress >= 0.5 else None
    if lock is not None:
        new_sl = round(entry + lock if is_long else entry - lock, 2)
        trade["trail_sl"] = max(trade["trail_sl"], new_sl) if is_long else min(trade["trail_sl"], new_sl)

    trailed = trade["trail_sl"] != trade["sl"]
    if (ltp >= target) if is_long else (ltp <= target):
        return "🎯 BOOK PROFIT", f"Price hit target {target}"
    if (ltp <= trade["trail_sl"]) if is_long else (ltp >= trade["trail_sl"]):
        if trailed:
            return "🔒 EXIT (trailing SL)", f"Gave back profit, trailing SL {trade['trail_sl']}"
        return "🛑 EXIT (stoploss)", f"Price hit SL {trade['sl']}"
    if (now.hour, now.minute) >= SQUARE_OFF_TIME:
        return "⏰ EXIT (square off)", "Intraday square-off time"

    # Failing trade: the reason for entry is gone, so get out before the stop
    minutes_in = (now - datetime.fromisoformat(trade["opened_ts"])).total_seconds() / 60
    back_in_range = ltp < trade["orh"] * (1 - 0.002) if is_long else ltp > trade["orl"] * (1 + 0.002)
    lost_vwap = (ltp < a["vwap"]) if is_long else (ltp > a["vwap"])
    if back_in_range:
        return "🚨 EXIT (breakout failed)", f"Price fell back inside the opening range ({round(trade['orl'], 2)}–{round(trade['orh'], 2)})"
    if lost_vwap and profit < 0:
        return "🚨 EXIT (lost VWAP)", f"Price crossed VWAP {round(a['vwap'], 2)} against the trade"
    if profit <= 0 and minutes_in >= STOCK_TIME_STOP_MINUTES and progress < 0.2:
        return "🚨 EXIT (not working)", f"{int(minutes_in)} min in with no progress"
    if a["signal"] and a["signal"] != trade["side"]:
        return "🚨 EXIT (signal reversed)", f"Setup flipped to {a['signal']}"
    if profit < 0:
        return "⚠️ HOLD (under water)", f"Below entry — SL {trade['trail_sl']}, VWAP {round(a['vwap'], 2)}"
    return "✅ HOLD", f"Target {target} / SL {trade['trail_sl']}"

def stock_trade_row(symbol, trade, ltp):
    price = trade["exit_price"] if trade["status"] != "OPEN" else ltp
    sign = 1 if trade["side"] == "LONG" else -1
    risk = abs(trade["entry"] - trade["sl"])
    return {
        "Stock": symbol,
        "Trade": f"{'📈 BUY' if sign == 1 else '📉 SELL'} {symbol}",
        "Entry": trade["entry"],
        "Entry Time": trade["opened_at"],
        "LTP": round(price, 2),
        "P&L %": round(sign * (price - trade["entry"]) / trade["entry"] * 100, 2),
        "Target": trade["target"],
        "Stoploss": trade["trail_sl"],
        "R:R": round(abs(trade["target"] - trade["entry"]) / risk, 2) if risk > 0 else None,
        "Exit Signal": trade["status"] if trade["status"] != "OPEN" else trade.get("status_live", "✅ HOLD"),
        "Reason": trade["reason"]
    }

# ---------- Shared display helpers ----------

def market_is_open(now):
    return now.weekday() < 5 and MARKET_OPEN_TIME <= (now.hour, now.minute) < SQUARE_OFF_TIME

def record_exit_status(trade, status, reason, price, now):
    """Close the trade on an exit signal, otherwise keep it open with the live status."""
    trade["reason"] = reason
    if status.startswith(("✅", "⚠️")):
        trade["status_live"] = status
    else:
        trade["status"], trade["exit_price"], trade["exit_ts"] = status, price, now.isoformat()

def show_exit_alerts(trades, now, label):
    for name, trade in trades.items():
        if trade["status"] != "OPEN" and trade.get("exit_ts"):
            age = (now - datetime.fromisoformat(trade["exit_ts"])).total_seconds() / 60
            if age <= EXIT_ALERT_MINUTES:
                alert = st.success if trade["status"].startswith("🎯") else st.error
                alert(f"**{trade['status']} — {label(name, trade)}** at {trade['exit_price']}: {trade['reason']}")
        elif trade["status"] == "OPEN" and trade.get("status_live", "").startswith("⚠️"):
            st.warning(f"**{trade['status_live']} — {label(name, trade)}**: {trade['reason']}")

def style_signals(df):
    def row_style(row):
        signal = str(row.get("Exit Signal", ""))
        if signal.startswith("🎯"):
            color = "rgba(34, 197, 94, 0.25)"
        elif signal.startswith(("🚨", "🛑", "🔒", "⏰")):
            color = "rgba(239, 68, 68, 0.25)"
        elif signal.startswith("⚠️"):
            color = "rgba(234, 179, 8, 0.25)"
        else:
            return [""] * len(row)
        return [f"background-color: {color}"] * len(row)
    return df.style.apply(row_style, axis=1) if "Exit Signal" in df.columns else df

# Trades are locked once signalled and tracked for the rest of the day (per browser session)
today = datetime.now(ist).strftime("%Y-%m-%d")
if st.session_state.get("trade_day") != today:
    st.session_state["trade_day"] = today
    st.session_state["trades"] = {}
    st.session_state["stock_trades"] = {}
st.session_state.setdefault("stock_trades", {})

@st.fragment(run_every=REFRESH_SECONDS)
def render_dashboard():
    now = datetime.now(ist)
    st.markdown(f"**Last Updated:** {now.strftime('%Y-%m-%d %H:%M:%S')} (IST) — auto-refresh every {REFRESH_SECONDS}s")

    trades = st.session_state["trades"]
    level_rows, trade_rows = [], []
    for symbol, (symbol_name, step) in indices.items():
        try:
            fetch = fetch_bse_option_chain if symbol_name in bse_scrip_codes else fetch_option_chain
            level_row, market = calculate_levels_and_trade(symbol_name, fetch(symbol_name), step)
            level_rows.append(level_row)
            if market is None:
                trade_rows.append({"Index": symbol_name, "Hero/Zero Trade": "❌ Error", "Reason": level_row.get("Error", "")})
                continue

            trade = trades.get(symbol_name)
            if trade is None and market["signal"] and market_is_open(now):
                trade = open_trade(symbol_name, market, now)
                if trade:
                    trades[symbol_name] = trade

            ltp = None
            if trade:
                ltp, _ = option_quote(market["records"], trade["strike"], trade["kind"])
                if trade["status"] == "OPEN" and isinstance(ltp, (int, float)):
                    status, reason = evaluate_exit(trade, ltp, market, now)
                    record_exit_status(trade, status, reason, ltp, now)
            row = trade_row(symbol_name, trade, ltp)
            if trade and trade["status"] == "OPEN":
                row["Exit Signal"] = trade.get("status_live", "✅ HOLD")
            trade_rows.append(row)
        except Exception as e:
            level_rows.append({"Index": symbol_name, "Error": str(e)})
            trade_rows.append({"Index": symbol_name, "Hero/Zero Trade": "❌ Error", "Reason": str(e)})

    show_exit_alerts(trades, now, lambda name, t: f"{name} {t['kind']} {t['strike']}")
    st.subheader("Trades & Exit Signals")
    st.dataframe(style_signals(pd.DataFrame(trade_rows)), width="stretch", hide_index=True)
    st.subheader("Levels")
    st.dataframe(pd.DataFrame(level_rows), width="stretch", hide_index=True)

@st.fragment(run_every=REFRESH_SECONDS)
def render_stocks():
    now = datetime.now(ist)
    extra = [s.strip().upper() for s in st.session_state.get("extra_symbols", "").split(",") if s.strip()]
    symbols = tuple(dict.fromkeys(NIFTY50 + extra))
    st.markdown(f"**Last Updated:** {now.strftime('%Y-%m-%d %H:%M:%S')} (IST) — {len(symbols)} stocks, "
                f"5-min candles from Yahoo Finance (may lag the exchange slightly)")

    try:
        candles = fetch_stock_candles(symbols)
    except Exception as e:
        st.error(f"Could not load stock data: {e}")
        return

    analysis = {}
    for symbol in symbols:
        try:
            a = analyse_stock(symbol, candles.get(symbol))
            if a:
                analysis[symbol] = a
        except Exception:
            continue
    if not analysis:
        st.error("No stock data available right now.")
        return

    trades = st.session_state["stock_trades"]
    open_count = sum(t["status"] == "OPEN" for t in trades.values())
    if market_is_open(now):
        fresh = sorted((a for s, a in analysis.items() if a["signal"] and s not in trades),
                       key=lambda a: a["strength"], reverse=True)
        for a in fresh[:max(0, MAX_STOCK_TRADES - open_count)]:
            trades[a["symbol"]] = plan_stock_trade(a, a["signal"], now)

    trade_rows = []
    for symbol, trade in trades.items():
        a = analysis.get(symbol)
        if a and trade["status"] == "OPEN":
            status, reason = evaluate_stock_exit(trade, a, now)
            record_exit_status(trade, status, reason, round(a["ltp"], 2), now)
        trade_rows.append(stock_trade_row(symbol, trade, a["ltp"] if a else trade["entry"]))

    show_exit_alerts(trades, now, lambda name, t: f"{'BUY' if t['side'] == 'LONG' else 'SELL'} {name}")
    st.subheader("Tracked Stock Trades & Exit Signals")
    if trade_rows:
        st.dataframe(style_signals(pd.DataFrame(trade_rows)), width="stretch", hide_index=True)
    else:
        st.info("No stock trades yet — signals appear after the first 15 minutes when a stock breaks its "
                "opening range with VWAP, pivot and volume confirmation.")

    scanner = pd.DataFrame([{
        "Stock": s,
        "Signal": {"LONG": "📈 LONG", "SHORT": "📉 SHORT"}.get(a["signal"], "—"),
        "LTP": round(a["ltp"], 2),
        "Chg %": round(a["change_pct"], 2),
        "VWAP": round(a["vwap"], 2),
        "OR High": round(a["orh"], 2),
        "OR Low": round(a["orl"], 2),
        "RVOL": round(a["rvol"], 2),
        "Pivot": round(a["levels"]["Pivot"], 2),
        "R1": round(a["levels"]["R1"], 2),
        "S1": round(a["levels"]["S1"], 2),
        "Prev High": round(a["pdh"], 2),
        "Prev Low": round(a["pdl"], 2),
        "Strength": round(a["strength"], 2)
    } for s, a in analysis.items()]).sort_values(["Signal", "Strength"], ascending=[False, False])
    st.subheader("Scanner")
    st.dataframe(scanner, width="stretch", hide_index=True)

options_tab, stocks_tab = st.tabs(["📊 Index Options (Hero/Zero)", "📈 Intraday Stocks"])

with options_tab:
    if st.button("🔄 Reset tracked trades", key="reset_options"):
        st.session_state["trades"] = {}
    render_dashboard()

with stocks_tab:
    left, right = st.columns([3, 1])
    left.text_input("Add more NSE symbols (comma separated, NIFTY 50 is included)", key="extra_symbols",
                    placeholder="e.g. IRCTC, DIXON, PERSISTENT")
    if right.button("🔄 Reset stock trades", key="reset_stocks"):
        st.session_state["stock_trades"] = {}
    render_stocks()
