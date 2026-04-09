import requests
import time
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# =========================
# TELEGRAM (SAFE + CHECK)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise Exception("❌ BOT_TOKEN or CHAT_ID missing in Railway variables")

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

SG_TZ = ZoneInfo("Asia/Singapore")

# =========================
# SETTINGS
# =========================
WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL = "5m"
LIMIT = 200
SCAN_INTERVAL = 300

SESSION_START = 21
SESSION_END = 2

EMA_FAST = 9
EMA_SLOW = 21
VOLUME_LOOKBACK = 20
SIDEWAYS_THRESHOLD = 0.001

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    try:
        requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# =========================
# EMA
# =========================
def ema(data, period):
    k = 2 / (period + 1)
    ema_vals = [data[0]]
    for p in data[1:]:
        ema_vals.append(p * k + ema_vals[-1] * (1 - k))
    return ema_vals

# =========================
# FETCH DATA
# =========================
def get_data(symbol):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": INTERVAL, "limit": LIMIT}

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()

    data = r.json()

    closes = [float(x[4]) for x in data]
    volumes = [float(x[5]) for x in data]

    return closes, volumes

# =========================
# SIGNAL LOGIC (IMPROVED)
# =========================
def check_signal(symbol):
    closes, volumes = get_data(symbol)

    ema9 = ema(closes, EMA_FAST)
    ema21 = ema(closes, EMA_SLOW)

    price = closes[-1]

    # sideways filter
    if abs(ema9[-1] - ema21[-1]) / price < SIDEWAYS_THRESHOLD:
        return None

    # volume filter (stronger)
    avg_vol = sum(volumes[-VOLUME_LOOKBACK:]) / VOLUME_LOOKBACK
    if volumes[-1] < avg_vol * 1.1:
        return None

    # BUY (better confirmation)
    if (
        price > ema21[-1]
        and ema9[-1] > ema21[-1]
        and closes[-2] < ema9[-2]
        and closes[-1] > ema9[-1]
    ):
        return f"🟢 BUY {symbol} @ {round(price,2)}"

    # SELL
    if (
        price < ema21[-1]
        and ema9[-1] < ema21[-1]
        and closes[-2] > ema9[-2]
        and closes[-1] < ema9[-1]
    ):
        return f"🔴 SELL {symbol} @ {round(price,2)}"

    return None

# =========================
# SESSION FILTER
# =========================
def in_session():
    hour = datetime.now(SG_TZ).hour
    return hour >= SESSION_START or hour < SESSION_END

# =========================
# HEARTBEAT (NEW)
# =========================
def send_heartbeat(last_hour):
    now = datetime.now(SG_TZ).hour
    if now != last_hour:
        send_telegram("🤖 Bot alive")
        return now
    return last_hour

# =========================
# TELEGRAM COMMANDS
# =========================
def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 10}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print("getUpdates error:", e)
        return []


def build_status_message(last_scan_time, last_signals):
    now = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    session_text = "YES" if in_session() else "NO"
    watched = ", ".join(WATCHLIST)
    recent = "\n".join(last_signals[-5:]) if last_signals else "No recent signals"
    return (
        "📊 Bot Status\n"
        f"Time: {now}\n"
        f"Session active: {session_text}\n"
        f"Watchlist: {watched}\n"
        f"Interval: {INTERVAL}\n"
        f"Last scan: {last_scan_time if last_scan_time else 'Not yet'}\n"
        f"Recent signals:\n{recent}"
    )


def process_telegram_commands(last_update_id, last_scan_time, last_signals):
    updates = get_updates(None if last_update_id is None else last_update_id + 1)
    for item in updates:
        last_update_id = item["update_id"]
        message = item.get("message", {})
        text = (message.get("text") or "").strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != str(CHAT_ID):
            continue

        if text == "/status":
            send_telegram(build_status_message(last_scan_time, last_signals))
        elif text == "/test":
            send_telegram("✅ Test reply from bot")
        elif text == "/help":
            send_telegram("Available commands:\n/status - show bot status\n/test - test Telegram reply\n/help - show commands")

    return last_update_id

# =========================
# MAIN
# =========================
def main():
    print("Bot started...")
    send_telegram("✅ Bot running (Cloud)")

    last_heartbeat_hour = -1
    last_update_id = None
    last_scan_time = None
    last_signals = []

    while True:
        try:
            last_heartbeat_hour = send_heartbeat(last_heartbeat_hour)
            last_update_id = process_telegram_commands(last_update_id, last_scan_time, last_signals)

            if not in_session():
                print("Outside session")
                last_scan_time = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
                time.sleep(SCAN_INTERVAL)
                continue

            for symbol in WATCHLIST:
                signal = check_signal(symbol)

                if signal:
                    print(signal)
                    send_telegram(signal)
                    stamp = datetime.now(SG_TZ).strftime("%H:%M:%S")
                    last_signals.append(f"{stamp} - {signal}")
                    last_signals = last_signals[-10:]
                else:
                    print(f"{symbol}: no signal")

            last_scan_time = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            print("Error:", e)

        time.sleep(SCAN_INTERVAL)

# =========================
if __name__ == "__main__":
    main()
