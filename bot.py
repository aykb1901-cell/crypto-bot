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
US_TZ = ZoneInfo("America/New_York")

SYMBOLS_FILE = "runner_symbols.txt"

INTERVAL = "5m"
PREMARKET_PERIOD = "2d"

SCAN_SECONDS = 30
COMMAND_POLL_SECONDS = 3
ALERT_COOLDOWN_SECONDS = 600
MAX_TOP_GAPPERS = 10
BATCH_SIZE = 80

MIN_PRICE = 0.50
MAX_PRICE = 20.00
MIN_GAP_PCT = 8.0
MIN_PM_VOL = 150000
MIN_AVG_DAILY_VOL = 300000
MIN_REL_VOL = 1.5
MAX_CHASE_PCT = 0.06

LONG_SL_PCT = 0.07
LONG_TP1_PCT = 0.10
LONG_TP2_PCT = 0.20
LONG_TP3_PCT = 0.30

SHORT_SL_PCT = 0.05
SHORT_TP1_PCT = 0.05
SHORT_TP2_PCT = 0.10
SHORT_TP3_PCT = 0.15

ADD_ON_BUFFER_PCT = 0.02

FALLBACK_RUNNERS = [
    "ABEO", "ADTX", "AGRI", "ALCE", "APDN", "AREB", "ATER", "ATNF", "AULT",
    "AVTX", "BIOR", "BIVI", "BMEA", "CERO", "CETY", "CHSN", "CLEU", "CNSP",
    "COSM", "CTM", "DATS", "DHAI", "DRUG", "ENSC", "FFIE", "FGEN", "GCTK",
    "GDHG", "GFAI", "GNS", "GOVX", "HOTH", "ICU", "IFBD", "IMPP", "INDO",
    "ISPC", "JAGX", "JCSE", "KAVL", "LASE", "LUCY", "MIGI", "MIRA", "MLGO",
    "MULN", "MYNZ", "NAAS", "NCNA", "NEGG", "NERV", "NVOS", "OCEA", "ONCO",
    "OPTT", "PEGY", "PHUN", "PRZO", "RANI", "REVB", "RIME", "SBET", "SGBX",
    "SIDU", "SLNH", "SNTG", "SOBR", "SONM", "SPCB", "SPRC", "STSS", "SUGP",
    "SXTC", "TCON", "TCRT", "TIVC", "TOP", "TPST", "TRNR", "UCAR", "USEA",
    "WIMI", "WLGS", "XFOR", "XLO", "ZAPP"
]

last_alert = {}
last_update_id = None
last_scan_summary = "No scan yet"
last_top = []
last_universe_size = 0


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


def clean_single_df(df: pd.DataFrame):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(col[0]) for col in df.columns]
    else:
        df.columns = [str(col) for col in df.columns]

    needed = ["Open", "High", "Low", "Close", "Volume"]
    for col in needed:
        if col not in df.columns:
            return None

    df = df[needed].dropna()
    if df.empty:
        return None

    return df


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def session_active() -> bool:
    now_us = datetime.now(US_TZ)
    total = now_us.hour * 60 + now_us.minute
    return 240 <= total <= 960


def is_premarket() -> bool:
    now_us = datetime.now(US_TZ)
    total = now_us.hour * 60 + now_us.minute
    return 240 <= total < 570


def load_symbols():
    if os.path.exists(SYMBOLS_FILE):
        with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
            rows = [x.strip().upper() for x in f.readlines()]

        out = []
        for s in rows:
            if not s or s.startswith("#"):
                continue
            if " " in s or "/" in s:
                continue
            out.append(s)

        return sorted(set(out))

    return FALLBACK_RUNNERS[:]


def get_data(symbol: str, interval="5m", period="2d", prepost=False):
    try:
        df = yf.download(
            tickers=symbol,
            interval=interval,
            period=period,
            progress=False,
            auto_adjust=False,
            threads=False,
            group_by="column",
            prepost=prepost,
        )
        return clean_single_df(df)
    except Exception as e:
        print(f"get_data error {symbol}: {e}")
        return None


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
    spy = get_data("SPY", interval="1m", period="1d", prepost=False)
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


def get_bulk_daily_stats(symbols):
    stats = {}

    for batch in chunked(symbols, BATCH_SIZE):
        tickers_str = " ".join(batch)

        try:
            df = yf.download(
                tickers=tickers_str,
                period="20d",
                interval="1d",
                progress=False,
                auto_adjust=False,
                threads=False,
                group_by="ticker",
                prepost=False,
            )

            if df is None or df.empty:
                continue

            if isinstance(df.columns, pd.MultiIndex):
                level0 = set(df.columns.get_level_values(0))
                for sym in batch:
                    try:
                        if sym not in level0:
                            continue
                        one = clean_single_df(df[sym])
                        if one is None or len(one) < 3:
                            continue

                        prev_close = float(one["Close"].iloc[-2])
                        avg_vol = float(one["Volume"].tail(10).mean())

                        stats[sym] = {
                            "prev_close": prev_close,
                            "avg_daily_vol": avg_vol,
                        }
                    except Exception:
                        continue
            else:
                if len(batch) == 1:
                    sym = batch[0]
                    one = clean_single_df(df)
                    if one is not None and len(one) >= 3:
                        stats[sym] = {
                            "prev_close": float(one["Close"].iloc[-2]),
                            "avg_daily_vol": float(one["Volume"].tail(10).mean()),
                        }

        except Exception as e:
            print("bulk daily stats error:", e)
            continue

    return stats


def get_premarket_snapshot(symbol: str):
    df = get_data(symbol, interval=INTERVAL, period=PREMARKET_PERIOD, prepost=True)
    if df is None or len(df) < 5:
        return None

    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(US_TZ)
        else:
            df.index = df.index.tz_convert(US_TZ)
    except Exception:
        pass

    today = datetime.now(US_TZ).date()

    pm = df[
        (df.index.date == today) &
        (df.index.hour >= 4) &
        ((df.index.hour < 9) | ((df.index.hour == 9) & (df.index.minute < 30)))
    ].copy()

    if pm.empty:
        return None

    pm_volume = float(pm["Volume"].sum())
    pm_high = float(pm["High"].max())
    pm_low = float(pm["Low"].min())
    last_price = float(pm["Close"].iloc[-1])
    pm_open = float(pm["Open"].iloc[0])

    return {
        "df": pm,
        "pm_volume": pm_volume,
        "pm_high": pm_high,
        "pm_low": pm_low,
        "last_price": last_price,
        "pm_open": pm_open,
    }


def build_trade_levels(side: str, entry: float, breakout_level: float):
    if side == "BUY":
        return {
            "sl": entry * (1 - LONG_SL_PCT),
            "tp1": entry * (1 + LONG_TP1_PCT),
            "tp2": entry * (1 + LONG_TP2_PCT),
            "tp3": entry * (1 + LONG_TP3_PCT),
            "add_on": breakout_level * (1 + ADD_ON_BUFFER_PCT),
            "be_after": entry * (1 + LONG_TP1_PCT),
        }

    return {
        "sl": entry * (1 + SHORT_SL_PCT),
        "tp1": entry * (1 - SHORT_TP1_PCT),
        "tp2": entry * (1 - SHORT_TP2_PCT),
        "tp3": entry * (1 - SHORT_TP3_PCT),
        "add_on": breakout_level * (1 - ADD_ON_BUFFER_PCT),
        "be_after": entry * (1 - SHORT_TP1_PCT),
    }


def bad_day_filter(pm_df: pd.DataFrame):
    if pm_df is None or len(pm_df) < 8:
        return True, "Not enough PM candles"

    recent = pm_df.tail(8)
    avg_range = float((recent["High"] - recent["Low"]).mean())
    avg_close = float(recent["Close"].mean())

    if avg_close <= 0:
        return True, "Invalid price"

    volatility_pct = avg_range / avg_close
    if volatility_pct < 0.004:
        return True, "Too little movement"

    return False, "OK"


def get_ranked_runners(symbols):
    ranked = []
    daily_stats = get_bulk_daily_stats(symbols)

    for symbol in symbols:
        try:
            daily = daily_stats.get(symbol)
            if not daily:
                continue

            prev_close = daily["prev_close"]
            avg_daily_vol = daily["avg_daily_vol"]

            if prev_close <= 0 or avg_daily_vol < MIN_AVG_DAILY_VOL:
                continue

            snap = get_premarket_snapshot(symbol)
            if not snap:
                continue

            last_price = snap["last_price"]
            pm_volume = snap["pm_volume"]

            if last_price < MIN_PRICE or last_price > MAX_PRICE:
                continue

            gap_pct = ((last_price - prev_close) / prev_close) * 100.0
            rel_vol = pm_volume / avg_daily_vol if avg_daily_vol > 0 else 0.0

            if gap_pct < MIN_GAP_PCT:
                continue
            if pm_volume < MIN_PM_VOL:
                continue
            if rel_vol < MIN_REL_VOL:
                continue

            ranked.append({
                "symbol": symbol,
                "gap_pct": gap_pct,
                "last_price": last_price,
                "pm_volume": pm_volume,
                "pm_high": snap["pm_high"],
                "pm_low": snap["pm_low"],
                "prev_close": prev_close,
                "avg_daily_vol": avg_daily_vol,
                "rel_vol": rel_vol,
            })

        except Exception as e:
            print(f"runner rank error {symbol}: {e}")
            continue

    ranked.sort(
        key=lambda x: (x["gap_pct"], x["pm_volume"], x["rel_vol"]),
        reverse=True
    )
    return ranked[:MAX_TOP_GAPPERS]


def build_runner_trade(symbol: str, snap: dict, spy_trend: str):
    pm = snap["df"].copy()

    blocked, reason = bad_day_filter(pm)
    if blocked:
        return {"blocked": True, "reason": reason}

    pm["EMA5"] = pm["Close"].ewm(span=5, adjust=False).mean()
    pm["EMA10"] = pm["Close"].ewm(span=10, adjust=False).mean()
    pm["EMA20"] = pm["Close"].ewm(span=20, adjust=False).mean()
    pm["RSI"] = calc_rsi(pm["Close"], 14)

    current = pm.iloc[-1]
    prev1 = pm.iloc[-2]
    prev2 = pm.iloc[-3]

    price = float(current["Close"])
    open_price = float(current["Open"])
    vol = float(current["Volume"])
    avg_vol = float(pm["Volume"].tail(8).mean())
    ema5 = float(current["EMA5"])
    ema10 = float(current["EMA10"])
    ema20 = float(current["EMA20"])
    rsi = float(current["RSI"]) if pd.notna(current["RSI"]) else 50.0
    strength = candle_strength(current)
    volume_ratio = vol / avg_vol if avg_vol > 0 else 0.0

    pm_high = float(pm["High"].max())
    pm_low = float(pm["Low"].min())

    confidence = "B"
    if volume_ratio >= 2.0 and strength >= 0.60 and rsi >= 60:
        confidence = "A+"
    elif volume_ratio >= 1.5 and strength >= 0.45 and rsi >= 55:
        confidence = "A"

    clean_bull = price > ema5 > ema10 > ema20
    breakout_ok = price >= pm_high * 0.995 and price > float(prev1["High"])
    pullback_hold = float(prev1["Low"]) >= float(prev1["EMA5"]) or float(prev2["Low"]) >= float(prev2["EMA5"])
    momentum_ok = rsi >= 55 and strength >= 0.35 and price > open_price
    not_chasing = price <= pm_high * (1 + MAX_CHASE_PCT)

    if (
        spy_trend != "BEAR"
        and clean_bull
        and breakout_ok
        and pullback_hold
        and momentum_ok
        and volume_ratio >= 1.2
        and not_chasing
    ):
        levels = build_trade_levels("BUY", price, pm_high)
        return {
            "blocked": False,
            "side": "BUY",
            "entry": price,
            "sl": levels["sl"],
            "tp1": levels["tp1"],
            "tp2": levels["tp2"],
            "tp3": levels["tp3"],
            "add_on": levels["add_on"],
            "be_after": levels["be_after"],
            "confidence": confidence,
            "volume_ratio": volume_ratio,
            "rsi": rsi,
            "strength": strength,
            "pm_high": pm_high,
            "pm_low": pm_low,
            "notes": "runner breakout + EMA stack + pullback hold",
        }

    clean_bear = price < ema5 < ema10 < ema20
    breakdown_ok = price <= pm_low * 1.005 and price < float(prev1["Low"])
    pop_fail = float(prev1["High"]) >= float(prev1["EMA5"]) or float(prev2["High"]) >= float(prev2["EMA5"])
    downside_ok = rsi <= 45 and strength >= 0.35 and price < open_price
    not_overextended_short = price >= pm_low * (1 - MAX_CHASE_PCT)

    if (
        spy_trend != "BULL"
        and clean_bear
        and breakdown_ok
        and pop_fail
        and downside_ok
        and volume_ratio >= 1.2
        and not_overextended_short
    ):
        levels = build_trade_levels("SELL", price, pm_low)
        return {
            "blocked": False,
            "side": "SELL",
            "entry": price,
            "sl": levels["sl"],
            "tp1": levels["tp1"],
            "tp2": levels["tp2"],
            "tp3": levels["tp3"],
            "add_on": levels["add_on"],
            "be_after": levels["be_after"],
            "confidence": confidence,
            "volume_ratio": volume_ratio,
            "rsi": rsi,
            "strength": strength,
            "pm_high": pm_high,
            "pm_low": pm_low,
            "notes": "runner breakdown + EMA stack",
        }

    return None


def classify_top_item(item, spy_trend):
    symbol = item["symbol"]
    snap = get_premarket_snapshot(symbol)
    if not snap:
        return {
            "symbol": symbol,
            "bucket": "WATCH",
            "status": "no PM data",
            "trade": None,
            "item": item,
        }

    trade = build_runner_trade(symbol, snap, spy_trend)

    if trade and not trade.get("blocked"):
        conf = trade.get("confidence", "B")
        if conf == "A+":
            bucket = "A+"
        elif conf == "A":
            bucket = "A"
        else:
            bucket = "WATCH"

        status = f"{trade['side']} @ {trade['entry']:.2f}"
        return {
            "symbol": symbol,
            "bucket": bucket,
            "status": status,
            "trade": trade,
            "item": item,
        }

    if trade and trade.get("blocked"):
        return {
            "symbol": symbol,
            "bucket": "WATCH",
            "status": f"blocked ({trade['reason']})",
            "trade": trade,
            "item": item,
        }

    return {
        "symbol": symbol,
        "bucket": "WATCH",
        "status": "watch only",
        "trade": None,
        "item": item,
    }


def build_top_message(top_items):
    if not top_items:
        return "📋 TOP RUNNER UNIVERSE\nNo strong runners found"

    spy_trend = get_spy_trend()
    classified = [classify_top_item(item, spy_trend) for item in top_items]

    a_plus = [x for x in classified if x["bucket"] == "A+"]
    a_only = [x for x in classified if x["bucket"] == "A"]
    watch = [x for x in classified if x["bucket"] == "WATCH"]

    lines = ["📋 TOP RUNNER UNIVERSE"]

    lines.append("\n🔥 A+ ONLY")
    if a_plus:
        for x in a_plus:
            item = x["item"]
            lines.append(
                f"{x['symbol']} | Gap {item['gap_pct']:+.2f}% | PMVol {item['pm_volume']:.0f} | "
                f"RVOL {item['rel_vol']:.2f} | {x['status']}"
            )
    else:
        lines.append("None")

    lines.append("\n✅ A")
    if a_only:
        for x in a_only:
            item = x["item"]
            lines.append(
                f"{x['symbol']} | Gap {item['gap_pct']:+.2f}% | PMVol {item['pm_volume']:.0f} | "
                f"RVOL {item['rel_vol']:.2f} | {x['status']}"
            )
    else:
        lines.append("None")

    lines.append("\n👀 WATCH ONLY")
    if watch:
        for x in watch:
            item = x["item"]
            lines.append(
                f"{x['symbol']} | Gap {item['gap_pct']:+.2f}% | PMVol {item['pm_volume']:.0f} | "
                f"RVOL {item['rel_vol']:.2f} | {x['status']}"
            )
    else:
        lines.append("None")

    return "\n".join(lines)


def build_fulltop_message(top_items):
    if not top_items:
        return "📋 FULL TOP\nNo strong runners found"

    spy_trend = get_spy_trend()
    classified = [classify_top_item(item, spy_trend) for item in top_items]
    strong = [x for x in classified if x["bucket"] in {"A+", "A"}]

    if not strong:
        return "📋 FULL TOP\nNo A+ or A setups now"

    lines = ["📋 FULL TOP"]

    for x in strong:
        item = x["item"]
        trade = x["trade"]

        lines.append(
            f"\n{x['bucket']} | {x['symbol']} | {trade['side']}\n"
            f"Gap: {item['gap_pct']:+.2f}% | PMVol: {item['pm_volume']:.0f} | RVOL: {item['rel_vol']:.2f}\n"
            f"Entry: {trade['entry']:.2f}\n"
            f"SL: {trade['sl']:.2f}\n"
            f"TP1: {trade['tp1']:.2f}\n"
            f"TP2: {trade['tp2']:.2f}\n"
            f"TP3: {trade['tp3']:.2f}\n"
            f"Add-On: {trade['add_on']:.2f}\n"
            f"BE After: {trade['be_after']:.2f}\n"
            f"PM High: {trade['pm_high']:.2f} | PM Low: {trade['pm_low']:.2f}\n"
            f"RSI: {trade['rsi']:.1f} | VolRatio: {trade['volume_ratio']:.2f}x | Strength: {trade['strength']:.2f}"
        )

    return "\n".join(lines)


def build_status():
    now = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    ready = ", ".join([x["symbol"] for x in last_top]) if last_top else "Not yet"
    mode = "PREMARKET" if is_premarket() else "REGULAR/OTHER"

    return (
        "📊 BOT STATUS\n"
        f"Time: {now}\n"
        f"Mode: {mode}\n"
        f"Session Active: {'YES' if session_active() else 'NO'}\n"
        f"Runner Universe Size: {last_universe_size}\n"
        f"Top Runners: {ready}\n"
        f"Last Scan:\n{last_scan_summary}"
    )


def scan():
    global last_scan_summary, last_top, last_universe_size

    symbols = load_symbols()
    last_universe_size = len(symbols)

    spy_trend = get_spy_trend()
    ranked = get_ranked_runners(symbols)
    last_top = ranked

    if not ranked:
        last_scan_summary = f"{datetime.now(SG_TZ).strftime('%H:%M:%S')} - no runner setups"
        return

    alerts_sent = 0
    scan_lines = []

    for item in ranked:
        symbol = item["symbol"]

        try:
            snap = get_premarket_snapshot(symbol)
            if not snap:
                scan_lines.append(f"{symbol}: no PM data")
                continue

            trade = build_runner_trade(symbol, snap, spy_trend)

            if not trade:
                scan_lines.append(f"{symbol}: no setup")
                continue

            if trade.get("blocked"):
                scan_lines.append(f"{symbol}: blocked ({trade['reason']})")
                continue

            if trade.get("confidence") not in {"A+", "A"}:
                scan_lines.append(f"{symbol}: watch only ({trade['confidence']})")
                continue

            key = f"{symbol}_{trade['side']}"
            last_time = last_alert.get(key, 0)

            if time.time() - last_time < ALERT_COOLDOWN_SECONDS:
                scan_lines.append(f"{symbol}: cooldown")
                continue

            msg = (
                f"🔥 RUNNER SNIPER {symbol}\n"
                f"SPY Trend: {spy_trend}\n"
                f"Gap: {item['gap_pct']:+.2f}%\n"
                f"PM Vol: {item['pm_volume']:.0f}\n"
                f"RVOL: {item['rel_vol']:.2f}\n"
                f"Prev Close: {item['prev_close']:.2f}\n"
                f"PM High: {trade['pm_high']:.2f}\n"
                f"PM Low: {trade['pm_low']:.2f}\n"
                f"Side: {trade['side']}\n"
                f"Confidence: {trade['confidence']}\n"
                f"Entry: {trade['entry']:.2f}\n"
                f"TP1: {trade['tp1']:.2f} (take 50%)\n"
                f"TP2: {trade['tp2']:.2f} (take 30%)\n"
                f"TP3: {trade['tp3']:.2f} (runner)\n"
                f"SL: {trade['sl']:.2f}\n"
                f"Add-On: {trade['add_on']:.2f}\n"
                f"Move SL to BE after: {trade['be_after']:.2f}\n"
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
            print(f"scan error {symbol}: {e}")
            scan_lines.append(f"{symbol}: error")
            continue

    prefix = datetime.now(SG_TZ).strftime("%H:%M:%S")
    if alerts_sent == 0:
        last_scan_summary = f"{prefix} - no valid A/A+ setup\n" + "\n".join(scan_lines[:8])
    else:
        last_scan_summary = f"{prefix} - {alerts_sent} A/A+ alert(s) sent\n" + "\n".join(scan_lines[:8])


def process_commands():
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
                send("✅ Test reply from runner sniper bot")
            elif text == "/top":
                send(build_top_message(last_top or []))
            elif text == "/fulltop":
                send(build_fulltop_message(last_top or []))
            elif text == "/status":
                send(build_status())
            elif text == "/help":
                send(
                    "Commands:\n"
                    "/test - test bot\n"
                    "/top - top runner universe (A+, A, watch)\n"
                    "/fulltop - full levels for A+ and A only\n"
                    "/status - bot status\n"
                    "/help - command list"
                )

        except Exception as e:
            print("command error:", e)
            continue


def main():
    send("🚀 RUNNER UNIVERSE SNIPER BOT STARTED")
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
