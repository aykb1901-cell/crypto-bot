import os
import csv
import time
import json
import queue
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from websocket import WebSocketApp

# =========================================================
# PRO VERSION - ALERT BOT FOR MANUAL COINHAKO EXECUTION
# =========================================================
# What this bot does:
# - Uses Binance WebSocket for live 5m candle updates
# - Sends Telegram alerts with entry / SL / TP
# - Supports /status /test /help /pause /resume /force_scan
# - Tracks paper trades to CSV
# - Uses cooldown + duplicate blocking
# - Uses a simple risk model for a $2000 account
#
# What this bot does NOT do:
# - It does not place trades automatically
# - It does not guarantee profits
#
# Railway env vars needed:
# BOT_TOKEN=...
# CHAT_ID=...
# =========================================================

# =========================
# ENV / TELEGRAM
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("BOT_TOKEN or CHAT_ID missing in Railway variables")

TG_SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TG_UPDATES_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

# =========================
# GENERAL SETTINGS
# =========================
SG_TZ = ZoneInfo("Asia/Singapore")
WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL = "5m"
KLINE_LIMIT = 250

# Trading session in Singapore time
SESSION_START = 21
SESSION_END = 2

# Account / risk model
ACCOUNT_SIZE = 2000.0
RISK_PER_TRADE_PCT = 1.0
MAX_SIGNALS_PER_DAY = 4
MAX_SIMULTANEOUS_OPEN_PAPER_TRADES = 2
COOLDOWN_BARS = 4

# Strategy tuning
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_LOOKBACK = 20
VOLUME_SPIKE_MULTIPLIER = 1.15
SIDEWAYS_GAP_THRESHOLD = 0.0012
MAX_RISK_TO_STOP_PCT = 1.8
MIN_RISK_TO_STOP_PCT = 0.20
TP1_R = 1.0
TP2_R = 1.8
BREAKEVEN_AFTER_TP1 = True

# Files
STATE_FILE = "bot_state.json"
TRADE_LOG_FILE = "paper_trades.csv"

# Polling for commands
COMMAND_POLL_SECONDS = 5
HEARTBEAT_SECONDS = 3600
RECONNECT_DELAY = 5

# =========================
# GLOBAL RUNTIME STATE
# =========================
runtime_lock = threading.Lock()
price_queue = queue.Queue()

candles_by_symbol = {}
current_prices = {}
last_scan_time = None
last_heartbeat_ts = 0

# =========================
# STATE
# =========================
def now_sgt() -> datetime:
    return datetime.now(SG_TZ)


def today_sgt() -> str:
    return now_sgt().strftime("%Y-%m-%d")


def default_state() -> dict:
    return {
        "date": today_sgt(),
        "signals_sent": 0,
        "cooldowns": {},
        "paused": False,
        "last_update_id": None,
        "recent_signals": [],
        "paper_trades": [],
        "last_heartbeat_ts": 0,
    }


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        base = default_state()
        base.update(state)
        return base
    except Exception:
        return default_state()


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def reset_daily_state_if_needed(state: dict) -> dict:
    if state.get("date") != today_sgt():
        state["date"] = today_sgt()
        state["signals_sent"] = 0
        state["cooldowns"] = {}
        state["recent_signals"] = []
    return state

# =========================
# TELEGRAM
# =========================
def send_telegram(message: str) -> None:
    try:
        requests.post(
            TG_SEND_URL,
            json={"chat_id": CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as e:
        print("Telegram send error:", e)


def get_updates(offset=None):
    params = {"timeout": 10}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(TG_UPDATES_URL, params=params, timeout=20)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print("Telegram update error:", e)
        return []

# =========================
# INDICATORS
# =========================
def ema(values, period: int):
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, period: int = 14):
    if len(values) < period + 1:
        return [50.0] * len(values)

    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out = [50.0] * len(values)

    rs = avg_gain / avg_loss if avg_loss != 0 else 999999
    out[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(values)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 999999
        out[i] = 100 - (100 / (1 + rs))

    return out


def atr(candles, period: int = 14):
    if len(candles) < 2:
        return [0.0] * len(candles)

    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    out = [trs[0]]
    for i in range(1, len(trs)):
        if i < period:
            out.append(sum(trs[: i + 1]) / (i + 1))
        else:
            out.append(((out[-1] * (period - 1)) + trs[i]) / period)
    return out

# =========================
# DATA
# =========================
def fetch_historical_klines(symbol: str):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": INTERVAL, "limit": KLINE_LIMIT}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    candles = []
    for row in data:
        candles.append(
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[6]),
                "closed": True,
            }
        )
    return candles


def initialize_market_data():
    global candles_by_symbol, current_prices
    for symbol in WATCHLIST:
        candles_by_symbol[symbol] = fetch_historical_klines(symbol)
        current_prices[symbol] = candles_by_symbol[symbol][-1]["close"]
        print(f"Loaded {symbol} candles: {len(candles_by_symbol[symbol])}")

# =========================
# HELPERS
# =========================
def in_session() -> bool:
    hour = now_sgt().hour
    return hour >= SESSION_START or hour < SESSION_END


def format_price(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 100:
        return f"{price:,.3f}"
    if price >= 1:
        return f"{price:,.4f}"
    return f"{price:,.6f}"


def average_volume(candles, lookback: int, end_index_exclusive: int) -> float:
    start = max(0, end_index_exclusive - lookback)
    vols = [c["volume"] for c in candles[start:end_index_exclusive]]
    return sum(vols) / len(vols) if vols else 0.0


def count_open_paper_trades(state: dict) -> int:
    return sum(1 for t in state["paper_trades"] if t.get("status") == "OPEN")

# =========================
# SIGNAL ENGINE
# =========================
def build_signal(symbol: str, candles):
    closes = [c["close"] for c in candles]
    ema_fast_vals = ema(closes, EMA_FAST)
    ema_slow_vals = ema(closes, EMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)
    atr_vals = atr(candles, ATR_PERIOD)

    i = len(candles) - 1
    if i < 30:
        return None

    current = candles[i]
    prev1 = candles[i - 1]
    prev2 = candles[i - 2]
    price = current["close"]

    gap_pct = abs(ema_fast_vals[i] - ema_slow_vals[i]) / price
    if gap_pct < SIDEWAYS_GAP_THRESHOLD:
        return None

    avg_vol = average_volume(candles, VOLUME_LOOKBACK, i)
    if current["volume"] < avg_vol * VOLUME_SPIKE_MULTIPLIER:
        return None

    current_rsi = rsi_vals[i]
    current_atr = atr_vals[i]

    # LONG
    trend_up = price > ema_slow_vals[i] and ema_fast_vals[i] > ema_slow_vals[i]
    pullback_long = prev1["low"] <= ema_fast_vals[i - 1] or prev2["low"] <= ema_fast_vals[i - 2]
    breakout_long = current["close"] > current["open"] and current["close"] > prev1["high"]

    if trend_up and pullback_long and breakout_long and current_rsi >= 50:
        entry = price
        stop = min(prev1["low"], prev2["low"], ema_slow_vals[i]) - (current_atr * 0.20)
        if stop < entry:
            risk_pct = ((entry - stop) / entry) * 100
            if MIN_RISK_TO_STOP_PCT <= risk_pct <= MAX_RISK_TO_STOP_PCT:
                risk_amount = ACCOUNT_SIZE * (RISK_PER_TRADE_PCT / 100)
                position_size = risk_amount / ((entry - stop) / entry)
                tp1 = entry + (entry - stop) * TP1_R
                tp2 = entry + (entry - stop) * TP2_R
                return {
                    "symbol": symbol,
                    "side": "BUY",
                    "entry": entry,
                    "stop_loss": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                    "risk_pct": risk_pct,
                    "position_size": position_size,
                    "reason": f"Trend up + pullback + breakout + RSI {current_rsi:.1f}",
                    "bar_time": current["close_time"],
                }

    # SHORT
    trend_down = price < ema_slow_vals[i] and ema_fast_vals[i] < ema_slow_vals[i]
    pullback_short = prev1["high"] >= ema_fast_vals[i - 1] or prev2["high"] >= ema_fast_vals[i - 2]
    breakdown_short = current["close"] < current["open"] and current["close"] < prev1["low"]

    if trend_down and pullback_short and breakdown_short and current_rsi <= 50:
        entry = price
        stop = max(prev1["high"], prev2["high"], ema_slow_vals[i]) + (current_atr * 0.20)
        if stop > entry:
            risk_pct = ((stop - entry) / entry) * 100
            if MIN_RISK_TO_STOP_PCT <= risk_pct <= MAX_RISK_TO_STOP_PCT:
                risk_amount = ACCOUNT_SIZE * (RISK_PER_TRADE_PCT / 100)
                position_size = risk_amount / ((stop - entry) / entry)
                tp1 = entry - (stop - entry) * TP1_R
                tp2 = entry - (stop - entry) * TP2_R
                return {
                    "symbol": symbol,
                    "side": "SELL",
                    "entry": entry,
                    "stop_loss": stop,
                    "tp1": tp1,
                    "tp2": tp2,
                    "risk_pct": risk_pct,
                    "position_size": position_size,
                    "reason": f"Trend down + pullback + breakdown + RSI {current_rsi:.1f}",
                    "bar_time": current["close_time"],
                }

    return None


def build_signal_message(signal: dict) -> str:
    icon = "🟢" if signal["side"] == "BUY" else "🔴"
    return (
        f"{icon} {signal['side']} ALERT - {signal['symbol']}\n"
        f"Entry: {format_price(signal['entry'])}\n"
        f"SL: {format_price(signal['stop_loss'])} ({signal['risk_pct']:.2f}%)\n"
        f"TP1: {format_price(signal['tp1'])}\n"
        f"TP2: {format_price(signal['tp2'])}\n"
        f"Suggested size: ${signal['position_size']:.0f}\n"
        f"Why: {signal['reason']}\n"
        f"Manual execution on Coinhako only."
    )

# =========================
# PAPER TRADE LOGGING
# =========================
def ensure_trade_log_file():
    if os.path.exists(TRADE_LOG_FILE):
        return
    with open(TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "opened_at", "closed_at", "symbol", "side", "entry", "stop_loss",
            "tp1", "tp2", "exit_price", "status", "result", "pnl_r", "pnl_usd"
        ])


def append_closed_trade_to_csv(trade: dict):
    with open(TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade.get("opened_at"), trade.get("closed_at"), trade.get("symbol"), trade.get("side"),
            trade.get("entry"), trade.get("stop_loss"), trade.get("tp1"), trade.get("tp2"),
            trade.get("exit_price"), trade.get("status"), trade.get("result"), trade.get("pnl_r"),
            trade.get("pnl_usd")
        ])


def open_paper_trade(signal: dict, state: dict):
    trade = {
        "opened_at": now_sgt().strftime("%Y-%m-%d %H:%M:%S"),
        "closed_at": None,
        "symbol": signal["symbol"],
        "side": signal["side"],
        "entry": signal["entry"],
        "stop_loss": signal["stop_loss"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "exit_price": None,
        "status": "OPEN",
        "result": None,
        "pnl_r": None,
        "pnl_usd": None,
        "tp1_hit": False,
        "breakeven_active": False,
    }
    state["paper_trades"].append(trade)


def update_paper_trades(state: dict):
    changed = False
    risk_amount = ACCOUNT_SIZE * (RISK_PER_TRADE_PCT / 100)

    for trade in state["paper_trades"]:
        if trade["status"] != "OPEN":
            continue

        symbol = trade["symbol"]
        price = current_prices.get(symbol)
        if price is None:
            continue

        entry = trade["entry"]
        stop = trade["stop_loss"]
        tp1 = trade["tp1"]
        tp2 = trade["tp2"]
        side = trade["side"]

        # BUY side
        if side == "BUY":
            if (not trade["tp1_hit"]) and price >= tp1:
                trade["tp1_hit"] = True
                if BREAKEVEN_AFTER_TP1:
                    trade["breakeven_active"] = True
                send_telegram(f"🎯 TP1 HIT - {symbol} BUY @ {format_price(price)}")
                changed = True

            current_stop = entry if trade["breakeven_active"] else stop

            if price <= current_stop:
                trade["status"] = "CLOSED"
                trade["closed_at"] = now_sgt().strftime("%Y-%m-%d %H:%M:%S")
                trade["exit_price"] = price
                if trade["tp1_hit"] and trade["breakeven_active"] and price <= entry:
                    trade["result"] = "BREAKEVEN_AFTER_TP1"
                    trade["pnl_r"] = 0.5
                    trade["pnl_usd"] = round(risk_amount * 0.5, 2)
                else:
                    trade["result"] = "STOP_LOSS"
                    trade["pnl_r"] = -1.0
                    trade["pnl_usd"] = round(-risk_amount, 2)
                append_closed_trade_to_csv(trade)
                changed = True
                continue

            if price >= tp2:
                trade["status"] = "CLOSED"
                trade["closed_at"] = now_sgt().strftime("%Y-%m-%d %H:%M:%S")
                trade["exit_price"] = price
                trade["result"] = "TP2"
                trade["pnl_r"] = 1.4 if trade["tp1_hit"] else 1.8
                trade["pnl_usd"] = round(risk_amount * trade["pnl_r"], 2)
                append_closed_trade_to_csv(trade)
                send_telegram(f"🏁 TP2 HIT - {symbol} BUY @ {format_price(price)}")
                changed = True
                continue

        # SELL side
        elif side == "SELL":
            if (not trade["tp1_hit"]) and price <= tp1:
                trade["tp1_hit"] = True
                if BREAKEVEN_AFTER_TP1:
                    trade["breakeven_active"] = True
                send_telegram(f"🎯 TP1 HIT - {symbol} SELL @ {format_price(price)}")
                changed = True

            current_stop = entry if trade["breakeven_active"] else stop

            if price >= current_stop:
                trade["status"] = "CLOSED"
                trade["closed_at"] = now_sgt().strftime("%Y-%m-%d %H:%M:%S")
                trade["exit_price"] = price
                if trade["tp1_hit"] and trade["breakeven_active"] and price >= entry:
                    trade["result"] = "BREAKEVEN_AFTER_TP1"
                    trade["pnl_r"] = 0.5
                    trade["pnl_usd"] = round(risk_amount * 0.5, 2)
                else:
                    trade["result"] = "STOP_LOSS"
                    trade["pnl_r"] = -1.0
                    trade["pnl_usd"] = round(-risk_amount, 2)
                append_closed_trade_to_csv(trade)
                changed = True
                continue

            if price <= tp2:
                trade["status"] = "CLOSED"
                trade["closed_at"] = now_sgt().strftime("%Y-%m-%d %H:%M:%S")
                trade["exit_price"] = price
                trade["result"] = "TP2"
                trade["pnl_r"] = 1.4 if trade["tp1_hit"] else 1.8
                trade["pnl_usd"] = round(risk_amount * trade["pnl_r"], 2)
                append_closed_trade_to_csv(trade)
                send_telegram(f"🏁 TP2 HIT - {symbol} SELL @ {format_price(price)}")
                changed = True
                continue

    if changed:
        save_state(state)


def paper_trade_summary(state: dict) -> str:
    closed = [t for t in state["paper_trades"] if t.get("status") == "CLOSED"]
    if not closed:
        return "No closed paper trades yet"

    wins = sum(1 for t in closed if t.get("pnl_usd", 0) > 0)
    losses = sum(1 for t in closed if t.get("pnl_usd", 0) < 0)
    total = sum(t.get("pnl_usd", 0) or 0 for t in closed)
    win_rate = (wins / len(closed)) * 100 if closed else 0
    return (
        f"Closed trades: {len(closed)}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Paper PnL: ${total:.2f}"
    )

# =========================
# COMMANDS
# =========================
def build_status_message(state: dict) -> str:
    session_text = "YES" if in_session() else "NO"
    watched = ", ".join(WATCHLIST)
    recent = "\n".join(state.get("recent_signals", [])[-5:]) or "No recent signals"
    open_count = count_open_paper_trades(state)
    return (
        "📊 Bot Status\n"
        f"Time: {now_sgt().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Session active: {session_text}\n"
        f"Paused: {'YES' if state.get('paused') else 'NO'}\n"
        f"Watchlist: {watched}\n"
        f"Interval: {INTERVAL}\n"
        f"Signals sent today: {state.get('signals_sent', 0)}\n"
        f"Open paper trades: {open_count}\n"
        f"Last scan: {last_scan_time or 'Not yet'}\n\n"
        f"Recent signals:\n{recent}"
    )


def process_telegram_commands(state: dict):
    offset = state.get("last_update_id")
    updates = get_updates(None if offset is None else offset + 1)

    for item in updates:
        state["last_update_id"] = item["update_id"]
        message = item.get("message", {})
        text = (message.get("text") or "").strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != str(CHAT_ID):
            continue

        if text == "/status":
            send_telegram(build_status_message(state))
        elif text == "/test":
            send_telegram("✅ Test reply from bot")
        elif text == "/pause":
            state["paused"] = True
            save_state(state)
            send_telegram("⏸ Bot paused. It will still answer commands.")
        elif text == "/resume":
            state["paused"] = False
            save_state(state)
            send_telegram("▶️ Bot resumed.")
        elif text == "/forcescan":
            force_scan(state, ignore_session=True)
        elif text == "/paper":
            send_telegram("🧾 Paper trade summary\n" + paper_trade_summary(state))
        elif text == "/help":
            send_telegram(
                "Commands:\n"
                "/status - bot status\n"
                "/test - test reply\n"
                "/pause - pause alerts\n"
                "/resume - resume alerts\n"
                "/forcescan - scan now\n"
                "/paper - paper trade summary\n"
                "/help - show commands"
            )

    save_state(state)

# =========================
# WEBSOCKET
# =========================
def on_ws_message(ws, message):
    try:
        payload = json.loads(message)
        stream = payload.get("stream", "")
        data = payload.get("data", {})
        k = data.get("k", {})
        symbol = k.get("s")
        if not symbol:
            return

        current_prices[symbol] = float(k.get("c", 0.0))
        candle = {
            "open_time": int(k.get("t")),
            "open": float(k.get("o")),
            "high": float(k.get("h")),
            "low": float(k.get("l")),
            "close": float(k.get("c")),
            "volume": float(k.get("v")),
            "close_time": int(k.get("T")),
            "closed": bool(k.get("x")),
        }
        price_queue.put((symbol, candle))
    except Exception as e:
        print("WS message error:", e)


def on_ws_error(ws, error):
    print("WebSocket error:", error)


def on_ws_close(ws, code, msg):
    print("WebSocket closed:", code, msg)


def on_ws_open(ws):
    print("WebSocket opened")


def websocket_thread():
    streams = "/".join([f"{s.lower()}@kline_{INTERVAL}" for s in WATCHLIST])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    while True:
        try:
            ws = WebSocketApp(
                url,
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("WS fatal error:", e)
        print(f"Reconnecting websocket in {RECONNECT_DELAY}s...")
        time.sleep(RECONNECT_DELAY)

# =========================
# CORE SCAN
# =========================
def update_candle_store(symbol: str, candle: dict):
    candles = candles_by_symbol.get(symbol, [])
    if not candles:
        candles_by_symbol[symbol] = [candle]
        return

    if candles[-1]["open_time"] == candle["open_time"]:
        candles[-1] = candle
    elif candle["open_time"] > candles[-1]["open_time"]:
        candles.append(candle)
        if len(candles) > KLINE_LIMIT:
            candles.pop(0)


def register_signal(signal: dict, state: dict):
    stamp = now_sgt().strftime("%H:%M:%S")
    line = f"{stamp} - {signal['side']} {signal['symbol']} @ {format_price(signal['entry'])}"
    state["recent_signals"].append(line)
    state["recent_signals"] = state["recent_signals"][-10:]
    state["signals_sent"] += 1
    state["cooldowns"][f"{signal['symbol']}_{signal['side']}"] = signal["bar_time"]
    save_state(state)


def can_send_signal(signal: dict, state: dict) -> bool:
    if state.get("paused"):
        return False
    if state.get("signals_sent", 0) >= MAX_SIGNALS_PER_DAY:
        return False
    if count_open_paper_trades(state) >= MAX_SIMULTANEOUS_OPEN_PAPER_TRADES:
        return False

    key = f"{signal['symbol']}_{signal['side']}"
    last_bar = state.get("cooldowns", {}).get(key, 0)
    if signal["bar_time"] == last_bar:
        return False
    return True


def force_scan(state: dict, ignore_session=False):
    global last_scan_time
    if (not ignore_session) and (not in_session()):
        return

    state = reset_daily_state_if_needed(state)

    for symbol in WATCHLIST:
        candles = candles_by_symbol.get(symbol)
        if not candles or len(candles) < 40:
            continue

        signal = build_signal(symbol, candles)
        if not signal:
            continue

        if not can_send_signal(signal, state):
            continue

        msg = build_signal_message(signal)
        print(msg)
        send_telegram(msg)
        open_paper_trade(signal, state)
        register_signal(signal, state)

    last_scan_time = now_sgt().strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)

# =========================
# MAIN LOOP
# =========================
def main():
    global last_scan_time, last_heartbeat_ts

    ensure_trade_log_file()
    state = load_state()
    initialize_market_data()

    ws_thread = threading.Thread(target=websocket_thread, daemon=True)
    ws_thread.start()

    send_telegram("✅ Pro bot running (Cloud). Manual Coinhako execution only.")
    print("Bot started...")

    last_command_poll = 0.0

    while True:
        try:
            state = reset_daily_state_if_needed(state)

            # Process websocket updates
            processed_closed_bar = False
            while not price_queue.empty():
                symbol, candle = price_queue.get_nowait()
                update_candle_store(symbol, candle)
                if candle.get("closed"):
                    processed_closed_bar = True

            # Update paper trades on live prices
            update_paper_trades(state)

            # Check commands every few seconds
            if time.time() - last_command_poll >= COMMAND_POLL_SECONDS:
                process_telegram_commands(state)
                last_command_poll = time.time()

            # Heartbeat hourly
            if time.time() - state.get("last_heartbeat_ts", 0) >= HEARTBEAT_SECONDS:
                send_telegram(
                    "🤖 Bot heartbeat\n"
                    f"Time: {now_sgt().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Session active: {'YES' if in_session() else 'NO'}\n"
                    f"Signals today: {state.get('signals_sent', 0)}"
                )
                state["last_heartbeat_ts"] = time.time()
                save_state(state)

            # Scan only when a candle closes, and only in session unless forced via command
            if processed_closed_bar and in_session() and (not state.get("paused")):
                force_scan(state, ignore_session=True)
            elif not in_session():
                print("Outside session")

        except Exception as e:
            print("Main loop error:", e)

        time.sleep(1)


if __name__ == "__main__":
    main()
