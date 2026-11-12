"""
CRT scanner -> Telegram  (version 2)
- Weekly external structure gives the bias (close-based breaks only).
- Daily CRT is checked LIVE: the day's candle has swept the previous day's
  high/low and price is back inside. The candle is NOT closed yet.
- Alerts only when the CRT direction matches the weekly bias.
Run hourly (see crt.yml). Each alert is sent once per daily candle.
"""
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ================== SETTINGS ==================
# "fx" = candle day starts 17:00 New York. "crypto" = candle day starts 00:00 UTC.
INSTRUMENTS = {
    "XAU/USD": "fx", "BTC/USD": "crypto",
    "EUR/USD": "fx", "GBP/USD": "fx", "USD/JPY": "fx",
    "AUD/USD": "fx", "USD/CAD": "fx", "AUD/JPY": "fx",
}
SWING_N = 2             # weekly swing = highest/lowest of N weeks each side
MIN_HOURS_OPEN = 0      # ignore sweeps in the first X hours of the daily candle
SEND_AGAINST_WEEKLY = False   # True = also alert CRTs against the weekly bias
SEND_NEUTRAL = False          # True = also alert when weekly bias is neutral
# ==============================================

TD_KEY = os.environ["TD_KEY"]
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT"]
FORCE = os.environ.get("FORCE") == "1"   # manual test run: also sends a summary

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")
STATE_FILE = "state.json"


def send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT, "text": text}, timeout=30)


def fetch(symbol, interval, size):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": symbol, "interval": interval, "outputsize": size,
                "timezone": "UTC", "apikey": TD_KEY},
        timeout=30,
    ).json()
    if "values" not in r:
        raise RuntimeError(r.get("message", str(r)))
    return list(reversed(r["values"]))  # oldest first


def shift(t, kind):
    """Time moved so that the daily candle opens at 00:00."""
    if kind == "crypto":
        return t.astimezone(UTC)
    return t.astimezone(NY) + timedelta(hours=7)


def get_weekly_closed(symbol):
    now = datetime.now(UTC).date()
    bars = []
    for v in fetch(symbol, "1week", 80):
        d = datetime.strptime(v["datetime"][:10], "%Y-%m-%d").date()
        if d + timedelta(days=7) > now:   # week still running -> skip it
            continue
        bars.append((float(v["open"]), float(v["high"]),
                     float(v["low"]), float(v["close"])))
    return bars


def get_hourly(symbol):
    rows = []
    for v in fetch(symbol, "1h", 200):
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        rows.append((t, float(v["open"]), float(v["high"]),
                     float(v["low"]), float(v["close"])))
    return rows


def weekly_structure(bars, n=SWING_N):
    """Walk through closed weekly candles. A swing high/low is confirmed after
    n candles on each side. A break needs a CLOSE beyond the swing level."""
    H = [b[1] for b in bars]
    L = [b[2] for b in bars]
    C = [b[3] for b in bars]
    bias, sh, sl = "NEUTRAL", None, None
    for i in range(len(bars)):
        j = i - n
        if j >= n:
            if H[j] == max(H[j - n:j + n + 1]):
                sh = H[j]
            if L[j] == min(L[j - n:j + n + 1]):
                sl = L[j]
        if sh is not None and C[i] > sh:
            bias, sh = "BULLISH", None
        elif sl is not None and C[i] < sl:
            bias, sl = "BEARISH", None
    return bias, sh, sl   # sh / sl = latest confirmed swings not broken yet


def build_days(rows, kind):
    out = {}
    for t, o, h, l, c in rows:
        k = shift(t, kind).date()
        if k not in out:
            out[k] = [o, h, l, c]
        else:
            out[k][1] = max(out[k][1], h)
            out[k][2] = min(out[k][2], l)
            out[k][3] = c
    return [(k, *out[k]) for k in sorted(out)]


def live_crt(rows, kind, now):
    """Returns (side, swept_level, price_now, target, day_key) or None."""
    days = build_days(rows, kind)
    now_key = shift(now, kind).date()
    if len(days) < 2 or days[-1][0] != now_key:
        return None                       # market closed / no current candle
    if shift(now, kind).hour < MIN_HOURS_OPEN:
        return None
    _, _, ph, pl, _ = days[-2]            # previous daily candle
    _, _, ch, cl, cc = days[-1]           # today's candle so far (cc = price now)
    if ch > ph and cl >= pl and cc < ph:
        return "BEARISH", ph, cc, pl, now_key
    if cl < pl and ch <= ph and cc > pl:
        return "BULLISH", pl, cc, ph, now_key
    return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    keys = list(state)[-300:]
    with open(STATE_FILE, "w") as f:
        json.dump({k: state[k] for k in keys}, f)


def main():
    now = datetime.now(UTC)
    state, errors, summary = load_state(), [], []
    for sym, kind in INSTRUMENTS.items():
        name = sym.replace("/", "")
        try:
            bias, sh, sl = weekly_structure(get_weekly_closed(sym))
            time.sleep(8)                 # free plan: 8 requests per minute
            crt = live_crt(get_hourly(sym), kind, now)
            time.sleep(8)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        status = "no CRT"
        if crt:
            side, swept, price, target, dkey = crt
            status = f"{side} CRT (live)"
            aligned = side == bias
            allowed = aligned or (bias == "NEUTRAL" and SEND_NEUTRAL) \
                or (bias != "NEUTRAL" and not aligned and SEND_AGAINST_WEEKLY)
            key = f"{name}|D1|{dkey}|{side}"
            if allowed and key not in state:
                state[key] = now.isoformat()
                lvl = "PDH" if side == "BEARISH" else "PDL"
                tgt = "prior low" if side == "BEARISH" else "prior high"
                send(
                    f"🕯 D1 CRT LIVE (candle still open)\n{name} · {side}\n\n"
                    f"Swept {lvl}: {swept}\n"
                    f"Price now: {price} (back inside)\n"
                    f"Target ({tgt}): {target}\n\n"
                    f"Weekly bias: {bias} — "
                    f"{'ALIGNED' if aligned else 'NOT aligned'}\n"
                    f"⚠ Not confirmed. Check your CISD yourself."
                )
        swing = f"swing high {sh} / swing low {sl}"
        summary.append(f"{name}: weekly {bias} ({swing}) | D1: {status}")
        print(summary[-1])
    save_state(state)
    if errors:
        send("⚠️ Scan problems:\n" + "\n".join(errors))
    if FORCE:
        send("Manual scan summary:\n\n" + "\n".join(summary))


if __name__ == "__main__":
    main()
