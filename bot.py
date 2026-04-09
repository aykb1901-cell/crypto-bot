import os
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import pandas as pd


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("BOT_TOKEN or CHAT_ID missing")

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
UPDATES_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
SG_TZ = ZoneInfo("Asia/Singapore")

BASE = [
    "TSLA", "NVDA", "AMD", "COIN", "PLTR", "SOFI", "RIVN", "AMC", "NIO",
    "META", "AAPL", "MSFT", "AMZN", "NFLX", "HOOD", "MARA", "RIOT"
]

INTERVAL = "1m"
INTRADAY_PERIOD = "1d"
SCAN_SECONDS = 10
COMMAND_POLL_SECONDS = 3
ALERT_COOLDOWN_SECONDS = 300
MAX_TOP_GAPPERS = 3

last_alert = {}
last_update_id = None
last_scan_summary = "No scan yet"
last_top3 = []


def send(msg: str) -> None:
    try:
        requests.post(
            TG_URL,
            json={"chat_id": CHAT_ID, "text": msg[:4000]},
            timeout=15,
        )
    except Exception as e:
        print("Telegram send error:", e)


def get_updates(offset=None):
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset

    try:
        response = requests.get(UPDATES_URL, params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
        return payload.get("result", [])
    except Exception as e:
        print("Telegram update error:", e)
        return []


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(col[0]) for col in df.columns]
    else:
        df.columns = [str(col) for col in df.columns]
    return df


def clean_df(df: pd.DataFrame):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    df = df.copy()
    df = flatten_columns(df)
    df = df.dropna(how="all")

    needed = ["Open", "High", "Low", "Close", "Volume"]
    for col in needed:
        if col not in df.columns:
            return None

    df = df[needed].dropna()
    if df.empty:
        return None

    return df


def get_data(symbol: str, interval: str = INTERVAL, period: str = INTRADAY_PERIOD):
    try:
        df = yf.download(
            tickers=symbol,
            interval=interval,
            period=period,
            progress=False,
            auto_adjust=False,
            threads=False,
            group_by="column",
            prepost=False,
        )
        return clean_df(df)
    except Exception as e:
        print(f"Data error for {symbol}: {e}")
        return None


def session_active() -> bool:
    hour = datetime.now(SG_TZ).hour
    return hour >= 21 or hour < 2


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def candle_strength(row: pd.Series) -> float:
    high = float(row["High"])
    low = float(row["Low"])
    open_ = float(row["Open"])
    close = float(row["Close"])

    rng = high - low
    if rng <= 0:
        return 0.0

    body = abs(close - open_)
    return body / rng


def get_spy_trend() -> str:
    spy = get_data("SPY")
    if spy is None or len(spy) < 25:
        return "NEUTRAL"

    spy = spy.copy()
    spy["EMA9"] = spy["Close"].ewm(span=9, adjust=False).mean()
    spy["EMA21"] = spy["Close"].ewm(span=21, adjust=False).mean()

    ema9 = float(spy["EMA9"].iloc[-1])
    ema21 = float(spy["EMA21"].iloc[-1])

    if ema9 > ema21:
        return "BULL"
    if ema9 < ema21:
        return "BEAR"
    return "NEUTRAL"


def get_ranked_gappers():
    results = []

    for symbol in BASE:
        try:
            df = yf.download(
                tickers=symbol,
                period="2d",
                interval="1d",
                progress=False,
                auto_adjust=False,
                threads=False,
                group_by="column",
                prepost=False,
            )
            df = clean_df(df)
            if df is None or len(df) < 2:
                continue

            prev_close = float(df["Close"].iloc[-2])
            today_open = float(df["Open"].iloc[-1])

            if prev_close == 0:
                continue

            pct = ((today_open - prev_close) / prev_close) * 100

            if abs(pct) > 3:
                results.append((symbol, abs(pct), pct))
        except Exception as e:
            print(f"Gapper error for {symbol}: {e}")
            continue

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:MAX_TOP_GAPPERS]


def build_top_message(top3) -> str:
    if not top3:
        return "📋 TOP 3 TODAY\nNo strong gappers found"

    trend = get_spy_trend()
    lines = ["📋 TOP 3 TODAY"]

    for idx, item in enumerate(top3, start=1):
        symbol, _, signed_pct = item
        prefix = "+" if signed_pct >= 0 else ""
        trade = build_trade(symbol, trend)

        if trade and not trade.get("blocked"):
            status = f"{trade['side']} ready @ {trade['entry']:.2f}"
        elif trade and trade.get("blocked"):
            status = f"blocked ({trade['reason']})"
        else:
            status = "no setup yet"

        lines.append(f"{idx}. {symbol} ({prefix}{signed_pct:.2f}%) - {status}")

    return "\n".join(lines)


def bad_day_filter(spy_trend: str, df: pd.DataFrame):
    if spy_trend == "NEUTRAL":
        return True, "SPY neutral"

    if df is None or len(df) < 30:
        return True, "Not enough data"

    recent = df.tail(20)
    avg_range = float((recent["High"] - recent["Low"]).mean())
    avg_close = float(recent["Close"].mean())

    if avg_close == 0:
        return True, "Invalid price"

    volatility_pct = avg_range / avg_close
    if volatility_pct < 0.003:
        return True, "Too little movement"

    return False, "OK"


def build_trade(symbol: str, trend: str):
    df = get_data(symbol)
    if df is None or len(df) < 40:
        return None

    blocked, reason = bad_day_filter(trend, df)
    if blocked:
        return {"blocked": True, "reason": reason}

    df = df.copy()
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["RSI"] = calc_rsi(df["Close"], 14)

    opening = df.head(15)
    if opening.empty:
        return None

    or_high = float(opening["High"].max())
    or_low = float(opening["Low"].min())

    current = df.iloc[-1]
    prev1 = df.iloc[-2]
    prev2 = df.iloc[-3]

    price = float(current["Close"])
    open_price = float(current["Open"])
    vol = float(current["Volume"])
    avg_vol = float(df["Volume"].tail(20).mean())
    ema9 = float(current["EMA9"])
    ema21 = float(current["EMA21"])
    rsi = float(current["RSI"])

    if avg_vol <= 0:
        return None

    volume_ratio = vol / avg_vol
    strength = candle_strength(current)

    confidence = "B"
    if volume_ratio >= 2.0 and strength >= 0.60:
        confidence = "A+"
    elif volume_ratio >= 1.5 and strength >= 0.45:
        confidence = "A"

    risk = 0.35
    reward = 0.70

    clean_bull_trend = price > ema9 > ema21
    pullback_ok = float(prev1["Low"]) <= float(prev1["EMA9"]) or float(prev2["Low"]) <= float(prev2["EMA9"])
    breakout_ok = price > or_high and price > float(prev1["High"])
    momentum_ok = rsi >= 55 and strength >= 0.35 and price > open_price

    if trend == "BULL" and clean_bull_trend and pullback_ok and breakout_ok and momentum_ok:
        entry = price
        sl = min(float(prev1["Low"]), float(prev2["Low"]), ema21)
        if entry - sl < 0.15:
            sl = entry - risk
        tp = entry + reward
        return {
            "blocked": False,
            "side": "BUY",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "confidence": confidence,
            "volume_ratio": volume_ratio,
            "rsi": rsi,
            "strength": strength,
            "notes": "bull trend + pullback + breakout",
        }

    clean_bear_trend = price < ema9 < ema21
    pop_ok = float(prev1["High"]) >= float(prev1["EMA9"]) or float(prev2["High"]) >= float(prev2["EMA9"])
    breakdown_ok = price < or_low and price < float(prev1["Low"])
    momentum_down_ok = rsi <= 45 and strength >= 0.35 and price < open_price

    if trend == "BEAR" and clean_bear_trend and pop_ok and breakdown_ok and momentum_down_ok:
        entry = price
        sl = max(float(prev1["High"]), float(prev2["High"]), ema21)
        if sl - entry < 0.15:
            sl = entry + risk
        tp = entry - reward
        return {
            "blocked": False,
            "side": "SELL",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "confidence": confidence,
            "volume_ratio": volume_ratio,
            "rsi": rsi,
            "strength": strength,
            "notes": "bear trend + pop + breakdown",
        }

    return None


def scan() -> None:
    global last_scan_summary, last_top3

    trend = get_spy_trend()
    ranked = get_ranked_gappers()
    watchlist = [x[0] for x in ranked]
    last_top3 = ranked

    if not watchlist:
        last_scan_summary = f"{datetime.now(SG_TZ).strftime('%H:%M:%S')} - no gappers"
        return

    alerts_sent = 0
    scan_lines = []

    for symbol in watchlist:
        try:
            trade = build_trade(symbol, trend)

            if not trade:
                scan_lines.append(f"{symbol}: no setup")
                continue

            if trade.get("blocked"):
                scan_lines.append(f"{symbol}: blocked ({trade['reason']})")
                continue

            key = f"{symbol}_{trade['side']}"
            last_time = last_alert.get(key, 0)
            if time.time() - last_time < ALERT_COOLDOWN_SECONDS:
                scan_lines.append(f"{symbol}: cooldown")
                continue

            msg = (
                f"🔥 SMART SNIPER {symbol}\n"
                f"Trend: {trend}\n"
                f"Confidence: {trade['confidence']}\n"
                f"Entry: {trade['entry']:.2f}\n"
                f"TP: {trade['tp']:.2f}\n"
                f"SL: {trade['sl']:.2f}\n"
                f"Vol Ratio: {trade['volume_ratio']:.2f}x\n"
                f"RSI: {trade['rsi']:.1f}\n"
                f"Candle Strength: {trade['strength']:.2f}\n"
                f"Setup: {trade['notes']}"
            )

            send(msg)
            last_alert[key] = time.time()
            alerts_sent += 1
            scan_lines.append(f"{symbol}: alert sent ({trade['confidence']})")

        except Exception as e:
            print(f"Scan error for {symbol}: {e}")
            scan_lines.append(f"{symbol}: error")
            continue

    prefix = datetime.now(SG_TZ).strftime("%H:%M:%S")
    if alerts_sent == 0:
        last_scan_summary = f"{prefix} - no valid setup\n" + "\n".join(scan_lines[:5])
    else:
        last_scan_summary = f"{prefix} - {alerts_sent} alert(s) sent\n" + "\n".join(scan_lines[:5])


def build_status() -> str:
    now = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    ready = ", ".join([x[0] for x in last_top3]) if last_top3 else "Not yet"
    return (
        "📊 BOT STATUS\n"
        f"Time: {now}\n"
        f"Session Active: {'YES' if session_active() else 'NO'}\n"
        f"Universe Size: {len(BASE)}\n"
        f"Top 3 Ready: {ready}\n"
        f"Last Scan:\n{last_scan_summary}"
    )


def process_commands() -> None:
    global last_update_id

    updates = get_updates(None if last_update_id is None else last_update_id + 1)

    for item in updates:
        try:
            last_update_id = item.get("update_id")
            message = item.get("message", {})
            text = (message.get("text") or "").strip().lower()
            chat_id = str(message.get("chat", {}).get("id", ""))

            if chat_id != str(CHAT_ID):
                continue

            if text == "/test":
                send("✅ Test reply from smart sniper bot")
            elif text == "/top":
                send(build_top_message(get_ranked_gappers()))
            elif text == "/status":
                send(build_status())
            elif text == "/help":
                send(
                    "Commands:\n"
                    "/test - test bot\n"
                    "/top - top gappers + setup status\n"
                    "/status - bot status\n"
                    "/help - command list"
                )
        except Exception as e:
            print("Command error:", e)
            continue


def main() -> None:
    send("🚀 SMART SNIPER BOT STARTED")
    print("Bot started...")

    last_scan = 0.0

    while True:
        try:
            process_commands()

            if session_active() and time.time() - last_scan >= SCAN_SECONDS:
                scan()
                last_scan = time.time()
            elif not session_active():
                print("Outside US session")

        except Exception as e:
            print("Main loop error:", e)
            print(traceback.format_exc())
            send(f"⚠️ Bot error: {str(e)[:200]}")
            time.sleep(5)

        time.sleep(COMMAND_POLL_SECONDS)


if __name__ == "__main__":
    main()
