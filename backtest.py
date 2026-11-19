"""
Backtest: replay the last BACKTEST_DAYS of real KXBTC15M windows with the exact LIVE rules
(fair_value model, 5c min edge after fee, 15-85c prices, >= 2 min left, 1 contract, held to settlement)
using Kalshi 1-minute candles and Coinbase 1-minute BTC prices.

Gives the bot hundreds of trades of experience in minutes. Auto mode can go live on this evidence.
Honest limits: 1-minute data (the live bot polls every 5s), assumes a fill at the candle's closing ask,
and past edges can vanish. It is evidence, not a guarantee.
"""
import math
import os
import time
from datetime import datetime, timezone

DAYS = float(os.environ.get("BACKTEST_DAYS", "30"))
MIN_EDGE = float(os.environ.get("LIVE_MIN_EDGE", "0.05"))
MIN_PRICE = float(os.environ.get("LIVE_MIN_PRICE", "0.15"))
MAX_PRICE = float(os.environ.get("LIVE_MAX_PRICE", "0.85"))
MIN_MINUTES = float(os.environ.get("LIVE_MIN_MINUTES", "2"))


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fetch_btc_minutes(http_json, start, end):
    """{minute_start_ts: close} from Coinbase, 300 minutes per request."""
    out = {}
    t = start
    while t < end:
        t2 = min(t + 300 * 60, end)
        rows = http_json(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"
                         f"&start={_iso(t)}&end={_iso(t2)}")
        for r in rows:
            out[int(r[0])] = float(r[4])
        t = t2
        time.sleep(0.15)
    return out


def fetch_markets(http_json, base, series, start):
    out, cursor = [], ""
    for _ in range(50):
        d = http_json(f"{base}/markets?series_ticker={series}&min_close_ts={int(start)}&limit=1000"
                      + (f"&cursor={cursor}" if cursor else ""))
        out += d.get("markets", [])
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    seen = {m["ticker"] for m in out}
    try:   # older settled markets may only be listed under /historical
        cursor = ""
        for _ in range(50):
            d = http_json(f"{base}/historical/markets?series_ticker={series}&min_close_ts={int(start)}&limit=1000"
                          + (f"&cursor={cursor}" if cursor else ""))
            out += [m for m in d.get("markets", []) if m.get("ticker") not in seen]
            cursor = d.get("cursor") or ""
            if not cursor:
                break
    except Exception:
        pass
    return [m for m in out if (m.get("result") or "").lower() in ("yes", "no")]


def fetch_candles(http_json, base, series, m):
    o, c = int(_ts(m["open_time"])), int(_ts(m["close_time"]))
    q = f"?start_ts={o}&end_ts={c}&period_interval=1"
    try:
        d = http_json(f"{base}/series/{series}/markets/{m['ticker']}/candlesticks{q}")
        if d.get("candlesticks"):
            return d["candlesticks"]
    except Exception:
        pass
    # markets older than Kalshi's historical cutoff live under /historical
    return http_json(f"{base}/historical/markets/{m['ticker']}/candlesticks{q}").get("candlesticks", [])


def sigma_at(btc, t, lookback=60):
    rets = []
    for k in range(lookback):
        a, b = btc.get(t - (k + 1) * 60), btc.get(t - k * 60)
        if a and b:
            rets.append(math.log(b / a))
    if len(rets) < 15:
        return None
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) or None


def simulate_window(m, candles, btc, fair_prob, fee_fn):
    """Return one trade dict (first moment the live rules fire) or None."""
    strike = m.get("floor_strike")
    close = _ts(m["close_time"])
    if not strike:
        return None
    for cd in sorted(candles, key=lambda x: x["end_period_ts"]):
        t = int(cd["end_period_ts"])
        mins = (close - t) / 60
        if mins < MIN_MINUTES or mins > 10:
            continue
        minute = t - 60 - (t % 60)          # BTC candle that closed at ~t
        spot = btc.get(minute) or btc.get(minute - 60)
        sig = sigma_at(btc, minute)
        ya = _f(cd.get("yes_ask", {}).get("close_dollars"))
        yb = _f(cd.get("yes_bid", {}).get("close_dollars"))
        if not (spot and sig):
            continue
        p = fair_prob(spot, float(strike), mins, sig)
        if p is None:
            continue
        for side, prob, ask in (("yes", p, ya), ("no", 1 - p, (1 - yb) if yb else None)):
            if ask and MIN_PRICE <= ask <= MAX_PRICE and prob - ask - fee_fn(1, ask) >= MIN_EDGE:
                fee = fee_fn(1, ask)
                won = (m["result"].lower() == side)
                return {"ticker": m["ticker"], "side": side, "price": ask, "fee": fee,
                        "minutes_left": mins, "result": m["result"].lower(),
                        "pnl": (1 if won else 0) - ask - fee}
    return None


def _f(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def run(db, http_json, base, fair_prob, fee_fn, series="KXBTC15M", status=None):
    status = status if status is not None else {}
    db.q("""CREATE TABLE IF NOT EXISTS backtest(ticker TEXT PRIMARY KEY, side TEXT, price REAL, fee REAL,
            minutes_left REAL, result TEXT, pnl REAL)""")
    now = time.time()
    start = now - DAYS * 86400
    status.update(state="loading BTC history")
    btc = fetch_btc_minutes(http_json, start - 2 * 3600, now)
    status.update(state="loading Kalshi windows")
    markets = fetch_markets(http_json, base, series, start)
    status.update(state="replaying", windows=len(markets), done=0)
    for i, m in enumerate(markets):
        try:
            tr = simulate_window(m, fetch_candles(http_json, base, series, m), btc, fair_prob, fee_fn)
        except Exception:
            tr = None
        if tr:
            db.q("INSERT OR REPLACE INTO backtest VALUES(?,?,?,?,?,?,?)",
                 (tr["ticker"], tr["side"], tr["price"], tr["fee"], tr["minutes_left"], tr["result"], tr["pnl"]))
        status["done"] = i + 1
        time.sleep(0.08)
    status.update(state="done", finished=time.time())
    return stats(db)


def stats(db):
    try:
        rows = [r[0] for r in db.q("SELECT pnl FROM backtest")]
        wins = db.q("SELECT COUNT(*) FROM backtest WHERE side=result")[0][0]
    except Exception:
        return {"n": 0}
    n = len(rows)
    if not n:
        return {"n": 0}
    mean = sum(rows) / n
    se = math.sqrt(sum((x - mean) ** 2 for x in rows) / (n - 1)) / math.sqrt(n) if n > 1 else None
    return {"n": n, "win_rate": wins / n, "pnl": sum(rows), "cents": mean * 100,
            "se_cents": se * 100 if se else None,
            "edge": bool(se and n >= 300 and mean - 2 * se > 0)}
