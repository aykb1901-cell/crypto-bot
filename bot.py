# ==============================================
# US STOCK SNIPER ELITE+ FINAL BOT
# ==============================================
# FEATURES
# - Top 3 gappers before / during session
# - SPY trend filter
# - Opening range breakout
# - Auto entry / TP / SL
# - Confidence score (A+ / B)
# - Bad day filter
# - Telegram commands: /test /top /status /help
# ==============================================

import os
import time
import requests
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("BOT_TOKEN or CHAT_ID missing")

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
UPDATES_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
SG_TZ = ZoneInfo("Asia/Singapore")

BASE = [
    "TSLA","NVDA","AMD","COIN","PLTR","SOFI","RIVN","AMC","NIO",
    "META","AAPL","MSFT","AMZN","NFLX","HOOD","MARA","RIOT"
]

INTERVAL = "1m"
INTRADAY_PERIOD = "1d"
last_alert = {}
last_update_id = None
last_scan_summary = "No scan yet"
last_top3 = []

# ==============================================
def send(msg):
    try:
        requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": msg}, timeout=15)
    except Exception as e:
        print("Telegram send error:", e)

# ==============================================
def get_updates(offset=None):
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(UPDATES_URL, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print("Telegram update error:", e)
        return []

# ==============================================
def get_data(symbol, interval=INTERVAL, period=INTRADAY_PERIOD):
    try:
        df = yf.download(symbol, interval=interval, period=period, progress=False, auto_adjust=False)
        return df
    except Exception:
        return None

# ==============================================
def get_spy_trend():
    spy = get_data("SPY")
    if spy is None or len(spy) < 25:
        return "NEUTRAL"

    ema9 = spy['Close'].ewm(span=9).mean()
    ema21 = spy['Close'].ewm(span=21).mean()

    if ema9.iloc[-1] > ema21.iloc[-1]:
        return "BULL"
    if ema9.iloc[-1] < ema21.iloc[-1]:
        return "BEAR"
    return "NEUTRAL"

# ==============================================
def get_ranked_gappers():
    results = []

    for s in BASE:
        try:
            df = yf.download(s, period="2d", interval="1d", progress=False, auto_adjust=False)
            if df is None or len(df) < 2:
                continue

            prev_close = float(df.iloc[-2]['Close'])
            today_open = float(df.iloc[-1]['Open'])
            pct = ((today_open - prev_close) / prev_close) * 100

            if abs(pct) > 4:
                results.append((s, abs(pct), pct))
        except Exception:
            continue

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:3]

# ==============================================
def bad_day_filter(spy_trend, df):
    if spy_trend == "NEUTRAL":
        return True, "SPY neutral"

    if df is None or len(df) < 30:
        return True, "Not enough data"

    recent = df.tail(20)
    avg_range = (recent['High'] - recent['Low']).mean()
    avg_close = recent['Close'].mean()

    if avg_close == 0:
        return True, "Invalid price"

    volatility_pct = avg_range / avg_close
    if volatility_pct < 0.003:
        return True, "Too little movement"

    return False, "OK"

# ==============================================
def build_trade(symbol, trend):
    df = get_data(symbol)
    if df is None or len(df) < 30:
        return None

    blocked, reason = bad_day_filter(trend, df)
    if blocked:
        return {"blocked": True, "reason": reason}

    opening = df.head(15)
    or_high = float(opening['High'].max())
    or_low = float(opening['Low'].min())

    current = df.iloc[-1]
    price = float(current['Close'])
    vol = float(current['Volume'])
    avg_vol = float(df['Volume'].tail(20).mean())

    if avg_vol <= 0:
        return None

    volume_ratio = vol / avg_vol
    risk = 0.4
    reward = 0.8
    confidence = "B"

    if volume_ratio >= 2.5:
        confidence = "A+"
    elif volume_ratio >= 2.0:
        confidence = "A"

    # BUY
    if price > or_high and trend == "BULL":
        entry = price
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
            "or_high": or_high,
        }

    # SELL
    if price < or_low and trend == "BEAR":
        entry = price
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
            "or_low": or_low,
        }

    return None

# ==============================================
def build_top_message(top3):
    if not top3:
        return "📋 TOP 3 TODAY\nNo strong gappers found"

    lines = ["📋 TOP 3 TODAY"]
    for idx, item in enumerate(top3, start=1):
        sym, abs_pct, signed_pct = item
        direction = "+" if signed_pct >= 0 else ""
        lines.append(f"{idx}. {sym} ({direction}{signed_pct:.2f}%)")
    return "\n".join(lines)

# ==============================================
def scan():
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
        if key in last_alert and time.time() - last_alert[key] < 300:
            scan_lines.append(f"{symbol}: cooldown")
            continue

        if trade['confidence'] not in ["A+", "A"]:
            scan_lines.append(f"{symbol}: low confidence")
            continue

        msg = (
            f"🔥 ELITE+ SNIPER {symbol}\n"
            f"Trend: {trend}\n"
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

    if alerts_sent == 0:
        last_scan_summary = f"{datetime.now(SG_TZ).strftime('%H:%M:%S')} - no A/A+ setup\n" + "\n".join(scan_lines[:5])
    else:
        last_scan_summary = f"{datetime.now(SG_TZ).strftime('%H:%M:%S')} - {alerts_sent} alert(s) sent\n" + "\n".join(scan_lines[:5])

# ==============================================
def build_status():
    now = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    in_session = (21 <= datetime.now(SG_TZ).hour or datetime.now(SG_TZ).hour < 2)
    return (
        "📊 BOT STATUS\n"
        f"Time: {now}\n"
        f"Session Active: {'YES' if in_session else 'NO'}\n"
        f"Universe Size: {len(BASE)}\n"
        f"Top 3 Ready: {', '.join([x[0] for x in last_top3]) if last_top3 else 'Not yet'}\n"
        f"Last Scan:\n{last_scan_summary}"
    )

# ==============================================
def process_commands():
    global last_update_id
    updates = get_updates(None if last_update_id is None else last_update_id + 1)

    for item in updates:
        last_update_id = item["update_id"]
        message = item.get("message", {})
        text = (message.get("text") or "").strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != str(CHAT_ID):
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

# ==============================================
def main():
    send("🚀 ELITE+ FINAL BOT STARTED")
    print("Bot started...")

    last_scan = 0

    while True:
        try:
            process_commands()

            now = datetime.now(SG_TZ)
            if 21 <= now.hour or now.hour < 2:
                if time.time() - last_scan >= 60:
                    scan()
                    last_scan = time.time()
            else:
                print("Outside US session")

        except Exception as e:
            print("Main loop error:", e)
            send(f"⚠️ Bot error: {str(e)[:200]}")
            time.sleep(5)

        time.sleep(2)

# ==============================================
if __name__ == "__main__":
    main()
