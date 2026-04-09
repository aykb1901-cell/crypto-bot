import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = str(os.getenv("CHAT_ID", "")).strip()

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
SCAN_SECONDS = 60
COMMAND_POLL_SECONDS = 3
ALERT_COOLDOWN_SECONDS = 300

# US market in Singapore time:
# 9:30 PM to 4:00 AM during DST, 10:30 PM to 5:00 AM outside DST.
# Your earlier version used 21 to 2. Keeping it simple but wider:
SESSION_START = 21
SESSION_END = 4

last_alert = {}
last_update_id = None
last_scan_summary = "No scan yet"
last_top3 = []
bot_start_time = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")


# =========================
# TELEGRAM
# =========================
def send(msg: str) -> None:
    try:
        requests.post(
            TG_URL,
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception as e:
        print("Telegram send error:", e)


def get_updates(offset=None):
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset

    try:
        r = requests.get(UPDATES_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("result", [])
    except Exception as e:
        print("Telegram update error:", e)
        return []


# =========================
# DATA HELPERS
# =========================
def normalize_series(df, column_name: str):
    """
    yfinance can sometimes return Series or DataFrame columns
    in slightly inconsistent shapes. This normalizes it.
    """
    col = df[column_name]

    # If returned as DataFrame because of multi-index quirks, take first column
    if hasattr(col, "columns"):
        col = col.iloc[:, 0]

    return col.dropna()


def get_data(symbol: str, interval: str = INTERVAL, period: str = INTRADAY_PERIOD):
    try:
        df = yf.download(
            symbol,
            interval=interval,
            period=period,
            progress=False,
            auto_adjust=False,
            threads=False,
            prepost=False,
        )
        if df is None or df.empty:
            return None
        return df.dropna(how="all")
    except Exception as e:
        print(f"Data error for {symbol}: {e}")
        return None


def session_active() -> bool:
    hour = datetime.now(SG_TZ).hour
    if SESSION_START < SESSION_END:
        return SESSION_START <= hour < SESSION_END
    return hour >= SESSION_START or hour < SESSION_END


# =========================
# STRATEGY
# =========================
def get_spy_trend() -> str:
    spy = get_data("SPY")
    if spy is None or spy.empty or len(spy) < 25:
        return "NEUTRAL"

    close = normalize_series(spy, "Close")
    if len(close) < 25:
        return "NEUTRAL"

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    if float(ema9.iloc[-1]) > float(ema21.iloc[-1]):
        return "BULL"
    if float(ema9.iloc[-1]) < float(ema21.iloc[-1]):
        return "BEAR"
    return "NEUTRAL"


def get_ranked_gappers():
    results = []

    for symbol in BASE:
        try:
            df = yf.download(
                symbol,
                period="5d",
                interval="1d",
                progress=False,
                auto_adjust=False,
                threads=False,
            )
            if df is None or df.empty or len(df) < 2:
                continue

            close = normalize_series(df, "Close")
            open_ = normalize_series(df, "Open")

            if len(close) < 2 or len(open_) < 1:
                continue

            prev_close = float(close.iloc[-2])
            today_open = float(open_.iloc[-1])

            if prev_close == 0:
                continue

            pct = ((today_open - prev_close) / prev_close) * 100
            if abs(pct) >= 4:
                results.append((symbol, abs(pct), pct))
        except Exception as e:
            print(f"Gapper error for {symbol}: {e}")
            continue

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:3]


def bad_day_filter(spy_trend: str, df):
    if spy_trend == "NEUTRAL":
        return True, "SPY neutral"

    if df is None or df.empty or len(df) < 30:
        return True, "Not enough data"

    high = normalize_series(df, "High")
    low = normalize_series(df, "Low")
    close = normalize_series(df, "Close")

    recent_high = high.tail(20)
    recent_low = low.tail(20)
    recent_close = close.tail(20)

    if len(recent_high) < 20 or len(recent_low) < 20 or len(recent_close) < 20:
        return True, "Not enough data"

    avg_range = float((recent_high - recent_low).mean())
    avg_close = float(recent_close.mean())

    if avg_close == 0:
        return True, "Invalid price"

    volatility_pct = avg_range / avg_close
    if volatility_pct < 0.003:
        return True, "Too little movement"

    return False, "OK"


def build_trade(symbol: str, trend: str):
    df = get_data(symbol)
    if df is None or df.empty or len(df) < 30:
        return None

    blocked, reason = bad_day_filter(trend, df)
    if blocked:
        return {"blocked": True, "reason": reason}

    high = normalize_series(df, "High")
    low = normalize_series(df, "Low")
    close = normalize_series(df, "Close")
    volume = normalize_series(df, "Volume")

    if len(high) < 30 or len(low) < 30 or len(close) < 30 or len(volume) < 30:
        return None

    # Opening range = first 15 one-minute candles
    opening_high = high.head(15)
    opening_low = low.head(15)

    if len(opening_high) < 15 or len(opening_low) < 15:
        return None

    or_high = float(opening_high.max())
    or_low = float(opening_low.min())

    price = float(close.iloc[-1])
    vol = float(volume.iloc[-1])
    avg_vol = float(volume.tail(20).mean())

    if avg_vol <= 0:
        return None

    volume_ratio = vol / avg_vol
    risk = round(max(price * 0.003, 0.20), 2)
    reward = round(risk * 2, 2)

    confidence = "B"
    if volume_ratio >= 2.5:
        confidence = "A+"
    elif volume_ratio >= 2.0:
        confidence = "A"

    if price > or_high and trend == "BULL":
        entry = price
        sl = round(entry - risk, 2)
        tp = round(entry + reward, 2)
        return {
            "blocked": False,
            "side": "BUY",
            "entry": round(entry, 2),
            "sl": sl,
            "tp": tp,
            "confidence": confidence,
            "volume_ratio": volume_ratio,
        }

    if price < or_low and trend == "BEAR":
        entry = price
        sl = round(entry + risk, 2)
        tp = round(entry - reward, 2)
        return {
            "blocked": False,
            "side": "SELL",
            "entry": round(entry, 2),
            "sl": sl,
            "tp": tp,
            "confidence": confidence,
            "volume_ratio": volume_ratio,
        }

    return None


# =========================
# MESSAGES
# =========================
def build_top_message(top3) -> str:
    if not top3:
        return "📋 TOP 3 TODAY\nNo strong gappers found"

    lines = ["📋 TOP 3 TODAY"]
    for idx, item in enumerate(top3, start=1):
        sym, _, signed_pct = item
        prefix = "+" if signed_pct >= 0 else ""
        lines.append(f"{idx}. {sym} ({prefix}{signed_pct:.2f}%)")
    return "\n".join(lines)


def build_status() -> str:
    now = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    ready = ", ".join([x[0] for x in last_top3]) if last_top3 else "Not yet"

    return (
        "📊 BOT STATUS\n"
        f"Time: {now}\n"
        f"Started: {bot_start_time}\n"
        f"Session Active: {'YES' if session_active() else 'NO'}\n"
        f"Universe Size: {len(BASE)}\n"
        f"Top 3 Ready: {ready}\n"
        f"Last Scan:\n{last_scan_summary}"
    )


# =========================
# SCANNER
# =========================
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

        if trade["confidence"] not in ["A+", "A"]:
            scan_lines.append(f"{symbol}: low confidence")
            continue

        msg = (
            f"🔥 ELITE+ SNIPER {symbol}\n"
            f"Trend: {trend}\n"
            f"Side: {trade['side']}\n"
            f"Confidence: {trade['confidence']}\n"
            f"Entry: {trade['entry']:.2f}\n"
            f"TP: {trade['tp']:.2f}\n"
            f"SL: {trade['sl']:.2f}\n"
            f"Vol Ratio: {trade['volume_ratio']:.2f}x\n"
            f"Rule: Only take if still near entry and breakout is clean"
        )
        send(msg)
        last_alert[key] = time.time()
        alerts_sent += 1
        scan_lines.append(f"{symbol}: alert sent ({trade['confidence']})")

    prefix = datetime.now(SG_TZ).strftime('%H:%M:%S')
    if alerts_sent == 0:
        last_scan_summary = f"{prefix} - no A/A+ setup\n" + "\n".join(scan_lines[:5])
    else:
        last_scan_summary = f"{prefix} - {alerts_sent} alert(s) sent\n" + "\n".join(scan_lines[:5])


# =========================
# COMMANDS
# =========================
def process_commands() -> None:
    global last_update_id

    updates = get_updates(None if last_update_id is None else last_update_id + 1)

    for item in updates:
        last_update_id = item.get("update_id")

        message = item.get("message", {})
        text = (message.get("text") or "").strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != CHAT_ID:
            continue

        if text == "/test":
            send("✅ Test reply from ELITE+ bot")

        elif text == "/top":
            send(build_top_message(get_ranked_gappers()))

        elif text == "/status":
            send(build_status())

        elif text == "/help":
            send(
                "Commands:\n"
                "/test - test bot\n"
                "/top - show top 3 gappers\n"
                "/status - bot status\n"
                "/help - command list"
            )


# =========================
# MAIN
# =========================
def main() -> None:
    send("🚀 ELITE+ FINAL BOT STARTED")
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
            send(f"⚠️ Bot error: {str(e)[:200]}")
            time.sleep(5)

        time.sleep(COMMAND_POLL_SECONDS)


if __name__ == "__main__":
    main()
