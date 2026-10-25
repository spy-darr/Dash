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
st.caption("Live OI + Pivot Levels + Hero/Zero Trade (market-based Target / SL with live exit signals)")

ist = pytz.timezone("Asia/Kolkata")

indices = {
    "NIFTY": ("NIFTY", 50),
    "BANKNIFTY": ("BANKNIFTY", 100),
    "FINNIFTY": ("FINNIFTY", 50),
    "MIDCPNIFTY": ("MIDCPNIFTY", 50)
}

# Yahoo Finance tickers used to get the previous day's real High/Low/Close for pivots
yf_tickers = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS"
}

RISK_FREE = 0.07
FALLBACK_TARGET_PCT = 0.15   # used only when a market-based plan can't be built
FALLBACK_SL_PCT = 0.05
MAX_RISK_PCT = 0.35          # never risk more than this fraction of premium, even if support is far
SQUARE_OFF_TIME = (15, 15)
EXPIRY_DAY_EXIT_TIME = (14, 30)
REFRESH_SECONDS = 60

@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
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

    pivot = (high + low + close) / 3
    return {
        "Pivot": pivot,
        "R1": 2 * pivot - low,
        "S1": 2 * pivot - high,
        "R2": pivot + (high - low),
        "S2": pivot - (high - low),
        "R3": high + 2 * (pivot - low),
        "S3": low - 2 * (high - pivot),
        "Source": source
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

def evaluate_exit(trade, ltp, spot, ce_oi, pe_oi, now):
    """Decide hold/exit for an open trade on every refresh. Mutates trade (peak, trailing SL, status)."""
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

    # OI shift warning: the wall that supports the trade is moving against it
    max_ce = max(ce_oi.items(), key=lambda x: x[1])[0] if ce_oi else None
    max_pe = max(pe_oi.items(), key=lambda x: x[1])[0] if pe_oi else None
    if is_ce and ((max_ce and max_ce < trade["max_ce"]) or (max_pe and max_pe < trade["max_pe"])):
        return "⚠️ HOLD (OI turning)", f"OI walls shifted down (CE {max_ce}, PE {max_pe}) — consider tightening"
    if not is_ce and ((max_ce and max_ce > trade["max_ce"]) or (max_pe and max_pe > trade["max_pe"])):
        return "⚠️ HOLD (OI turning)", f"OI walls shifted up (CE {max_ce}, PE {max_pe}) — consider tightening"
    return "✅ HOLD", f"Target {target} / SL {trade['trail_sl']}"

def calculate_levels_and_trade(symbol, data, step):
    if "error" in data:
        return {"Index": symbol, "Spot": "—", "Error": data["error"]}, None

    records_all = data.get("records", {})
    spot = records_all.get("underlyingValue", None)
    if spot is None:
        return {"Index": symbol, "Spot": "—", "Error": "No Spot"}, None

    # Use only the nearest expiry so OI walls, prices and IV all refer to the same contract
    expiry = (records_all.get("expiryDates") or [None])[0]
    records = [r for r in records_all.get("data", []) if expiry is None or r.get("expiryDate") == expiry]

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
    if not isinstance(ltp, (int, float)) or ltp < 1 or not market["expiry"]:
        return None

    spot = market["spot"]
    target_spot, stop_spot = underlying_exit_levels(kind, spot, market["step"], market["levels"],
                                                    market["ce_oi"], market["pe_oi"])
    t = years_to_expiry(market["expiry"], now)
    target, sl, basis = plan_exit(kind, strike, ltp, iv, t, spot, target_spot, stop_spot)
    return {
        "kind": kind, "strike": strike, "expiry": market["expiry"],
        "entry": round(ltp, 2), "entry_spot": spot, "opened_at": now.strftime("%H:%M"),
        "target": target, "sl": sl, "trail_sl": sl, "basis": basis,
        "target_spot": target_spot, "stop_spot": stop_spot, "peak": ltp,
        "max_ce": market["max_ce"], "max_pe": market["max_pe"],
        "status": "OPEN", "exit_price": None, "reason": ""
    }

def trade_row(symbol, trade, ltp):
    if trade is None:
        return {"Index": symbol, "Hero/Zero Trade": "⚠️ Wait for Breakout"}
    price = trade["exit_price"] if trade["status"] != "OPEN" else ltp
    pnl = round((price - trade["entry"]) / trade["entry"] * 100, 1) if isinstance(price, (int, float)) else "—"
    risk = trade["entry"] - trade["sl"]
    return {
        "Index": symbol,
        "Hero/Zero Trade": f"{'📈' if trade['kind'] == 'CE' else '📉'} BUY {symbol} {trade['kind']} {trade['strike']}",
        "Entry": trade["entry"],
        "Entry Time": trade["opened_at"],
        "LTP": price if price is not None else "—",
        "P&L %": pnl,
        "Target": trade["target"],
        "Stoploss": trade["trail_sl"],
        "R:R": round((trade["target"] - trade["entry"]) / risk, 2) if risk > 0 else "—",
        "Spot Target": round(trade["target_spot"], 2),
        "Spot Stop": round(trade["stop_spot"], 2),
        "Plan": trade["basis"],
        "Exit Signal": trade["status"],
        "Reason": trade["reason"]
    }

# Trades are locked once signalled and tracked for the rest of the day (per browser session)
today = datetime.now(ist).strftime("%Y-%m-%d")
if st.session_state.get("trade_day") != today:
    st.session_state["trade_day"] = today
    st.session_state["trades"] = {}

if st.button("🔄 Reset tracked trades"):
    st.session_state["trades"] = {}

@st.fragment(run_every=REFRESH_SECONDS)
def render_dashboard():
    now = datetime.now(ist)
    st.markdown(f"**Last Updated:** {now.strftime('%Y-%m-%d %H:%M:%S')} (IST) — auto-refresh every {REFRESH_SECONDS}s")

    trades = st.session_state["trades"]
    level_rows, trade_rows = [], []
    for symbol, (symbol_name, step) in indices.items():
        try:
            level_row, market = calculate_levels_and_trade(symbol_name, fetch_option_chain(symbol_name), step)
            level_rows.append(level_row)
            if market is None:
                trade_rows.append({"Index": symbol_name, "Hero/Zero Trade": "❌ Error", "Reason": level_row.get("Error", "")})
                continue

            trade = trades.get(symbol_name)
            if trade is None and market["signal"]:
                trade = open_trade(symbol_name, market, now)
                if trade:
                    trades[symbol_name] = trade

            ltp = None
            if trade:
                ltp, _ = option_quote(market["records"], trade["strike"], trade["kind"])
                if trade["status"] == "OPEN" and isinstance(ltp, (int, float)):
                    status, reason = evaluate_exit(trade, ltp, market["spot"], market["ce_oi"], market["pe_oi"], now)
                    trade["reason"] = reason
                    if status.startswith("✅") or status.startswith("⚠️"):
                        trade["status_live"] = status
                    else:
                        trade["status"], trade["exit_price"] = status, ltp
            row = trade_row(symbol_name, trade, ltp)
            if trade and trade["status"] == "OPEN":
                row["Exit Signal"] = trade.get("status_live", "✅ HOLD")
            trade_rows.append(row)
        except Exception as e:
            level_rows.append({"Index": symbol_name, "Error": str(e)})
            trade_rows.append({"Index": symbol_name, "Hero/Zero Trade": "❌ Error", "Reason": str(e)})

    st.subheader("Trades & Exit Signals")
    st.dataframe(pd.DataFrame(trade_rows), use_container_width=True, hide_index=True)
    st.subheader("Levels")
    st.dataframe(pd.DataFrame(level_rows), use_container_width=True, hide_index=True)

render_dashboard()
