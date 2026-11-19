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
    return [m for m in out if (m.get("result") or "").lower() in ("yes", "no")
            and str(m.get("ticker", "")).startswith(series + "-")
            and m.get("close_time") and _ts(m["close_time"]) >= start]


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


def simulate_trend12(m, candles, fee_fn):
    """Nick's rule: at the 12-minutes-left mark, buy whichever side is leading, at the ask, hold to close."""
    close = _ts(m["close_time"])
    best = None
    for cd in candles:
        mins = (close - int(cd["end_period_ts"])) / 60
        if 11.5 <= mins <= 12.5:
            best = cd
            break
    if not best:
        return None
    ya = _f(best.get("yes_ask", {}).get("close_dollars"))
    yb = _f(best.get("yes_bid", {}).get("close_dollars"))
    if not (ya and yb):
        return None
    side = "yes" if (ya + yb) / 2 >= 0.5 else "no"
    ask = ya if side == "yes" else 1 - yb
    if not (0.01 < ask < 0.99):
        return None
    fee = fee_fn(1, ask)
    won = m["result"].lower() == side
    return {"ticker": m["ticker"], "side": side, "price": ask, "fee": fee, "minutes_left": 12,
            "result": m["result"].lower(), "pnl": (1 if won else 0) - ask - fee}


TAKE_PROFIT = float(os.environ.get("TAKE_PROFIT", "0.03"))


def simulate_trend12_tp(m, candles, fee_fn, tp=None):
    """Nick's rule + quick cash-out: enter leader at 12 min left; sell the first minute the bid locks in
    >= TAKE_PROFIT after both fees; otherwise hold to settlement."""
    tp = TAKE_PROFIT if tp is None else tp
    entry = simulate_trend12(m, candles, fee_fn)
    if not entry:
        return None
    close = _ts(m["close_time"])
    cost = entry["price"] + entry["fee"]
    for cd in sorted(candles, key=lambda x: x["end_period_ts"]):
        mins = (close - int(cd["end_period_ts"])) / 60
        if mins >= 11.5 or mins <= 0:
            continue
        if entry["side"] == "yes":
            bid = _f(cd.get("yes_bid", {}).get("close_dollars"))
        else:
            ya = _f(cd.get("yes_ask", {}).get("close_dollars"))
            bid = (1 - ya) if ya else None
        if bid:
            profit = bid - fee_fn(1, bid) - cost
            if profit >= tp:
                return dict(entry, pnl=profit, minutes_left=mins, result="sold")
    return entry


def build_ctx(m, candles, btc, fair_prob):
    """Per-minute view of a window for the strategy library."""
    close = _ts(m["close_time"])
    strike = m.get("floor_strike")
    out = []
    for cd in sorted(candles, key=lambda x: x["end_period_ts"]):
        t = int(cd["end_period_ts"])
        mins = (close - t) / 60
        if mins < 0.5:
            continue
        minute = t - 60 - (t % 60)
        spot = btc.get(minute) or btc.get(minute - 60)
        ya = _f(cd.get("yes_ask", {}).get("close_dollars"))
        yb = _f(cd.get("yes_bid", {}).get("close_dollars"))
        mid = (ya + yb) / 2 if ya and yb else None
        sig = sigma_at(btc, minute)
        def ret(k):
            a = btc.get(minute - k * 60)
            return (spot / a - 1) if (spot and a) else None
        c = {"t": t, "mins": mins, "ya": ya, "yb": yb, "mid": mid, "spot": spot,
             "p": fair_prob(spot, float(strike), mins, sig) if (spot and strike and sig) else None,
             "r1": ret(1), "r2": ret(2), "r15": ret(15)}
        c["dmid2"] = (mid - out[-2]["mid"]) if (mid is not None and len(out) >= 2 and out[-2]["mid"] is not None) else None
        out.append(c)
    return out


def run_library(db, m, ctx, prev, fee_fn):
    import strategies
    for name, fn in strategies.LIBRARY.items():
        try:
            r = fn(ctx, prev, fee_fn)
        except Exception:
            r = None
        if not r:
            continue
        side, c = r
        ask = c["ya"] if side == "yes" else 1 - c["yb"]
        fee = fee_fn(1, ask)
        won = m["result"].lower() == side
        db.q("INSERT OR REPLACE INTO backtest_multi VALUES(?,?,?,?)",
             (name, m["ticker"], (1 if won else 0) - ask - fee, 1 if won else 0))


def leaderboard(db):
    import math as _m
    try:
        rows = db.q("SELECT strategy, pnl, won FROM backtest_multi")
    except Exception:
        return []
    by = {}
    for name, pnl, won in rows:
        by.setdefault(name, []).append((pnl, won))
    out = []
    for name, xs in by.items():
        n = len(xs)
        mean = sum(p for p, _ in xs) / n
        se = _m.sqrt(sum((p - mean) ** 2 for p, _ in xs) / (n - 1)) / _m.sqrt(n) if n > 1 else 0
        out.append({"name": name, "n": n, "win": sum(w for _, w in xs) / n, "cents": mean * 100,
                    "lo": (mean - 2 * se) * 100, "hi": (mean + 2 * se) * 100,
                    "verdict": ("EDGE" if n >= 300 and mean - 2 * se > 0 else
                                "LOSES" if n >= 100 and mean + 2 * se < 0 else "unclear")})
    return sorted(out, key=lambda r: r["cents"], reverse=True)


def _f(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def run(db, http_json, base, fair_prob, fee_fn, series="KXBTC15M", status=None):
    status = status if status is not None else {}
    db.q("CREATE TABLE IF NOT EXISTS backtest_multi(strategy TEXT, ticker TEXT, pnl REAL, won INTEGER,"
         " PRIMARY KEY(strategy, ticker))")
    for tbl in ("backtest", "backtest_trend12", "backtest_trend12_tp"):
        db.q(f"""CREATE TABLE IF NOT EXISTS {tbl}(ticker TEXT PRIMARY KEY, side TEXT, price REAL, fee REAL,
                minutes_left REAL, result TEXT, pnl REAL)""")
    now = time.time()
    start = now - DAYS * 86400
    status.update(state="loading BTC history")
    btc = fetch_btc_minutes(http_json, start - 2 * 3600, now)
    status.update(state="loading Kalshi windows")
    markets = fetch_markets(http_json, base, series, start)
    markets.sort(key=lambda x: _ts(x["close_time"]))
    status.update(state="replaying", windows=len(markets), done=0)
    prev_by_close = {_ts(x["close_time"]): x["result"].lower() for x in markets}
    for i, m in enumerate(markets):
        try:
            cds = fetch_candles(http_json, base, series, m)
        except Exception:
            cds = []
        try:
            run_library(db, m, build_ctx(m, cds, btc, fair_prob), prev_by_close.get(_ts(m["open_time"])), fee_fn)
        except Exception:
            pass
        for tbl, fn in (("backtest", lambda: simulate_window(m, cds, btc, fair_prob, fee_fn)),
                        ("backtest_trend12", lambda: simulate_trend12(m, cds, fee_fn)),
                        ("backtest_trend12_tp", lambda: simulate_trend12_tp(m, cds, fee_fn))):
            try:
                tr = fn()
            except Exception:
                tr = None
            if tr:
                db.q(f"INSERT OR REPLACE INTO {tbl} VALUES(?,?,?,?,?,?,?)",
                     (tr["ticker"], tr["side"], tr["price"], tr["fee"], tr["minutes_left"], tr["result"], tr["pnl"]))
        status["done"] = i + 1
        time.sleep(0.08)
    status.update(state="done", finished=time.time())
    return stats(db)


def stats(db, table="backtest"):
    try:
        rows = [r[0] for r in db.q(f"SELECT pnl FROM {table}")]
        wins = db.q(f"SELECT COUNT(*) FROM {table} WHERE pnl > 0")[0][0]
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
