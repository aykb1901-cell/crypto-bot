import requests
import time
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# =========================
# TELEGRAM (from Railway env)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

SG_TZ = ZoneInfo("Asia/Singapore")

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL = "5m"
LIMIT = 200
SCAN_INTERVAL = 300

SESSION_START = 21
SESSION_END = 2

EMA_FAST = 9
EMA_SLOW = 21
VOLUME_LOOKBACK = 20

# =========================
def send_telegram(msg):
    requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": msg})

# =========================
def ema(data, period):
    k = 2 / (period + 1)
    ema_vals = [data[0]]
    for p in data[1:]:
        ema_vals.append(p * k + ema_vals[-1] * (1 - k))
    return ema_vals

# =========================
def get_data(symbol):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": INTERVAL, "limit": LIMIT}
    data = requests.get(url, params=params).json()
    
    closes = [float(x[4]) for x in data]
    volumes = [float(x[5]) for x in data]
    
    return closes, volumes

# =========================
def check_signal(symbol):
    closes, volumes = get_data(symbol)
    
    ema9 = ema(closes, EMA_FAST)
    ema21 = ema(closes, EMA_SLOW)
    
    price = closes[-1]
    
    # sideways filter
    if abs(ema9[-1] - ema21[-1]) / price < 0.001:
        return None
    
    # volume filter
    avg_vol = sum(volumes[-20:]) / 20
    if volumes[-1] < avg_vol:
        return None
    
    # BUY
    if price > ema21[-1] and closes[-2] < ema9[-2] and closes[-1] > ema9[-1]:
        return f"🟢 BUY {symbol} @ {price}"
    
    # SELL
    if price < ema21[-1] and closes[-2] > ema9[-2] and closes[-1] < ema9[-1]:
        return f"🔴 SELL {symbol} @ {price}"
    
    return None

# =========================
def in_session():
    hour = datetime.now(SG_TZ).hour
    return hour >= SESSION_START or hour < SESSION_END

# =========================
def main():
    send_telegram("✅ Bot running (Cloud)")
    
    while True:
        try:
            if not in_session():
                time.sleep(SCAN_INTERVAL)
                continue
            
            for symbol in WATCHLIST:
                signal = check_signal(symbol)
                if signal:
                    print(signal)
                    send_telegram(signal)
            
        except Exception as e:
            print("Error:", e)
        
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
