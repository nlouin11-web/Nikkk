"""
BTC 15-minute paper trader for Kalshi KXBTC15M.

PAPER ONLY: no account, no API key, no real orders. Watches every live
15-minute Bitcoin window, runs several strategies with fake money, settles
them against Kalshi's official result, and shows profit/loss after fees on a
phone-friendly dashboard.

Standard library only. Run:  python main.py
"""
import csv
import html
import io
import json
import math
import os
import random
import sqlite3
import threading
import time
import traceback
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- config
KALSHI = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
SERIES = os.environ.get("SERIES", "KXBTC15M")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
CONTRACTS = int(os.environ.get("CONTRACTS", "10"))
FEE_RATE = 0.07            # Kalshi taker fee: 0.07 * C * P * (1-P), rounded up to the cent
MIN_SETTLED_FOR_VERDICT = 300
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH") or ("/data/bot.db" if os.path.isdir("/data") else "bot.db")


# ---------------------------------------------------------------- math helpers
def taker_fee(contracts, price):
    """Kalshi taker fee in dollars, rounded UP to the next cent."""
    raw = FEE_RATE * contracts * price * (1 - price)
    return math.ceil(round(raw * 100, 6)) / 100


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fair_yes_prob(spot, strike, minutes_left, sigma_per_min):
    """Chance BTC finishes >= strike, assuming a random walk with the recent volatility.
    Settlement averages the last 60s, which removes ~half a minute of variance."""
    if not (spot and strike and sigma_per_min) or sigma_per_min <= 0:
        return None
    t = max(minutes_left - 0.5, 0.05)
    z = math.log(spot / strike) / (sigma_per_min * math.sqrt(t))
    return norm_cdf(z)


def to_float(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- data sources
def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "btc15-paper/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def btc_spot():
    """BTC/USD from Coinbase, Kraken as backup. (Kalshi settles on CF Benchmarks BRTI,
    which is built from exchanges like these, so it tracks closely.)"""
    try:
        return float(http_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    except Exception:
        d = http_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")["result"]
        return float(next(iter(d.values()))["c"][0])


def current_market(now):
    data = http_json(f"{KALSHI}/markets?series_ticker={SERIES}&status=open&limit=20")
    live = []
    for m in data.get("markets", []):
        try:
            close, opened = parse_ts(m["close_time"]), parse_ts(m["open_time"])
        except Exception:
            continue
        if opened <= now < close:
            live.append((close, m))
    return min(live, key=lambda x: x[0])[1] if live else None


def get_market(ticker):
    return http_json(f"{KALSHI}/markets/{ticker}").get("market", {})


# ---------------------------------------------------------------- volatility
class Vol:
    """Per-minute log-return volatility of BTC from ~1-minute samples."""

    def __init__(self):
        self.samples = deque(maxlen=180)  # (minute, price)

    def add(self, ts, price):
        minute = int(ts // 60)
        if self.samples and self.samples[-1][0] == minute:
            self.samples[-1] = (minute, price)
        else:
            self.samples.append((minute, price))

    def sigma(self, lookback=60):
        s = list(self.samples)[-(lookback + 1):]
        rets = [math.log(b[1] / a[1]) / math.sqrt(b[0] - a[0])
                for a, b in zip(s, s[1:]) if b[0] > a[0] and a[1] > 0]
        if len(rets) < 15:
            return None
        mean = sum(rets) / len(rets)
        return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) or None

    def change(self, minutes):
        s = list(self.samples)
        if len(s) < 2:
            return None
        last = s[-1]
        past = [p for p in s if p[0] <= last[0] - minutes]
        return (last[1] / past[-1][1] - 1) if past else None


# ---------------------------------------------------------------- strategies
# Each gets the window state and returns ("yes"|"no", reason) to buy at the ask, or None.
# One trade per strategy per window, held to settlement.

def s_random(st):
    if 12 <= st["min_left"] <= 14:
        return (random.choice(["yes", "no"]), "coin flip (baseline)")


def s_momentum_open(st):
    if 12 <= st["min_left"] <= 14 and st["btc_15m_change"] is not None:
        side = "yes" if st["btc_15m_change"] >= 0 else "no"
        return side, f"BTC {st['btc_15m_change']*100:+.3f}% last 15m"


def s_leader_8m(st):
    if 7 <= st["min_left"] <= 8 and st["yes_mid"] is not None:
        side = "yes" if st["yes_mid"] >= 0.5 else "no"
        if st["ask"][side] and st["ask"][side] <= 0.90:
            return side, f"leader at {st['yes_mid']:.2f} mid"


EDGE = float(os.environ.get("FAIR_EDGE", "0.04"))


def s_fair_value(st):
    p = st["p_yes"]
    if p is None or not (1 <= st["min_left"] <= 10):
        return None
    for side, prob in (("yes", p), ("no", 1 - p)):
        ask = st["ask"][side]
        if ask and ask <= 0.97:
            edge = prob - ask - taker_fee(1, ask)
            if edge >= EDGE:
                return side, f"model {prob:.2f} vs ask {ask:.2f} (edge {edge*100:.1f}c)"


STRATEGIES = {
    "random_baseline": s_random,
    "momentum_open": s_momentum_open,
    "leader_8min": s_leader_8m,
    "fair_value": s_fair_value,
}


# ---------------------------------------------------------------- storage
class DB:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS trades(
          id INTEGER PRIMARY KEY, ts REAL, strategy TEXT, ticker TEXT, close_ts REAL,
          side TEXT, price REAL, contracts INTEGER, fee REAL, reason TEXT,
          result TEXT, pnl REAL, UNIQUE(strategy, ticker));
        CREATE TABLE IF NOT EXISTS snapshots(
          ts REAL, ticker TEXT, yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
          spot REAL, strike REAL, p_yes REAL);
        """)
        self.c.commit()

    def q(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur.fetchall()


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, db):
        self.db = db
        self.vol = Vol()
        self.status = {"started": time.time(), "last_ok": None, "error": None}

    def snapshot_state(self, m, now, spot):
        close = parse_ts(m["close_time"])
        yb, ya = to_float(m.get("yes_bid_dollars")), to_float(m.get("yes_ask_dollars"))
        nb, na = to_float(m.get("no_bid_dollars")), to_float(m.get("no_ask_dollars"))
        strike = to_float(m.get("floor_strike"))
        mins = (close - now) / 60
        sigma = self.vol.sigma()
        return {
            "ticker": m["ticker"], "close_ts": close, "min_left": mins,
            "spot": spot, "strike": strike, "sigma": sigma,
            "yes_bid": yb, "no_bid": nb,
            "ask": {"yes": ya, "no": na},
            "yes_mid": (yb + ya) / 2 if yb and ya else None,
            "p_yes": fair_yes_prob(spot, strike, mins, sigma),
            "btc_15m_change": self.vol.change(15),
        }

    def trade(self, name, st, side, reason, now=None):
        price = st["ask"][side]
        if not price or price >= 0.99:
            return
        self.db.q("INSERT OR IGNORE INTO trades(ts,strategy,ticker,close_ts,side,price,contracts,fee,reason)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (now or time.time(), name, st["ticker"], st["close_ts"], side, price,
                   CONTRACTS, taker_fee(CONTRACTS, price), reason))

    def settle(self, now, fetch=get_market):
        rows = self.db.q("SELECT DISTINCT ticker FROM trades WHERE result IS NULL AND close_ts < ?", (now - 3,))
        for (ticker,) in rows:
            res = (fetch(ticker).get("result") or "").lower()
            if res in ("yes", "no"):
                self.db.q("UPDATE trades SET result=?, pnl = contracts*(CASE WHEN side=? THEN 1 ELSE 0 END)"
                          " - contracts*price - fee WHERE ticker=? AND result IS NULL", (res, res, ticker))

    def step(self, now=None, market_fn=current_market, spot_fn=btc_spot, fetch=get_market):
        now = now or time.time()
        spot = spot_fn()
        self.vol.add(now, spot)
        self.settle(now, fetch)
        m = market_fn(now)
        if not m:
            self.status.update(window=None)
            return None
        st = self.snapshot_state(m, now, spot)
        self.db.q("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                  (now, st["ticker"], st["yes_bid"], st["ask"]["yes"], st["no_bid"], st["ask"]["no"],
                   spot, st["strike"], st["p_yes"]))
        for name, fn in STRATEGIES.items():
            try:
                d = fn(st)
            except Exception:
                d = None
            if d:
                self.trade(name, st, *d, now=now)
        self.status.update(window=st, last_ok=now, error=None)
        return st

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:
                self.status["error"] = f"{type(e).__name__}: {e}"
                print("step error:", self.status["error"], flush=True)
            time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- stats + dashboard
def strategy_stats(db):
    out = []
    for name in STRATEGIES:
        rows = db.q("SELECT pnl, contracts, result=side FROM trades WHERE strategy=? AND result IS NOT NULL", (name,))
        open_n = db.q("SELECT COUNT(*) FROM trades WHERE strategy=? AND result IS NULL", (name,))[0][0]
        n = len(rows)
        per = [r[0] / r[1] for r in rows]  # $ per contract
        mean = sum(per) / n if n else 0
        se = (math.sqrt(sum((x - mean) ** 2 for x in per) / (n - 1)) / math.sqrt(n)) if n > 1 else None
        if n < MIN_SETTLED_FOR_VERDICT:
            verdict = f"Need more data ({n}/{MIN_SETTLED_FOR_VERDICT})"
        elif se and mean - 2 * se > 0:
            verdict = "EDGE LOOKS REAL"
        elif se and mean + 2 * se < 0:
            verdict = "Loses money"
        else:
            verdict = "No clear edge"
        out.append({
            "strategy": name, "settled": n, "open": open_n,
            "win_rate": (sum(1 for r in rows if r[2]) / n) if n else None,
            "pnl": sum(r[0] for r in rows), "cents_per_contract": mean * 100,
            "se_cents": se * 100 if se else None, "verdict": verdict,
        })
    return out


def fmt_money(x):
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def render_dashboard(engine):
    db = engine.db
    stats = strategy_stats(db)
    st = engine.status.get("window")
    now = time.time()
    rows = ""
    for s in stats:
        color = "#16a34a" if s["pnl"] > 0 else "#dc2626" if s["pnl"] < 0 else "#666"
        wr = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "–"
        se = f" ±{2*s['se_cents']:.1f}" if s["se_cents"] else ""
        rows += (f"<tr><td><b>{s['strategy']}</b><br><small>{html.escape(s['verdict'])}</small></td>"
                 f"<td>{s['settled']}<small> (+{s['open']})</small></td><td>{wr}</td>"
                 f"<td style='color:{color}'><b>{fmt_money(s['pnl'])}</b><br>"
                 f"<small>{s['cents_per_contract']:+.2f}¢{se}</small></td></tr>")
    recent = db.q("SELECT ts,strategy,ticker,side,price,result,pnl,reason FROM trades ORDER BY id DESC LIMIT 25")
    trs = ""
    for ts, strat, tk, side, price, res, pnl, reason in recent:
        t = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")
        p = fmt_money(pnl) if pnl is not None else "open"
        trs += (f"<tr><td>{t}<br><small>{html.escape(tk[-7:])}</small></td><td>{strat}</td>"
                f"<td>{side.upper()} @{price:.2f}<br><small>{html.escape(reason or '')}</small></td>"
                f"<td>{res or ''} {p}</td></tr>")
    if st:
        live = (f"<b>{st['ticker']}</b> · {st['min_left']:.1f} min left<br>"
                f"BTC ${st['spot']:,.2f} vs target {('$%s' % format(st['strike'], ',.2f')) if st['strike'] else '?'}<br>"
                f"YES ask {st['ask']['yes'] or '–'} · NO ask {st['ask']['no'] or '–'}"
                + (f" · model YES {st['p_yes']:.2f}" if st['p_yes'] is not None else " · model warming up"))
    else:
        live = "No open window right now."
    err = engine.status.get("error")
    age = f"{now - engine.status['last_ok']:.0f}s ago" if engine.status.get("last_ok") else "never"
    return f"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=30><title>BTC15 Paper Bot</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:12px;background:#0b0f14;color:#e6e6e6}}
h1{{font-size:20px;margin:0 0 4px}} .card{{background:#151b23;border-radius:10px;padding:10px;margin:10px 0}}
table{{width:100%;border-collapse:collapse;font-size:14px}} td,th{{padding:6px 4px;border-bottom:1px solid #222;text-align:left;vertical-align:top}}
small{{color:#8b949e}} .err{{color:#f87171}}</style></head><body>
<h1>BTC 15-min Paper Bot</h1><small>PAPER MONEY ONLY · {CONTRACTS} contracts/trade · fees included · updated {age}</small>
{f'<div class="card err">Error: {html.escape(err)}</div>' if err else ''}
<div class=card>{live}</div>
<div class=card><table><tr><th>Strategy</th><th>Trades</th><th>Win%</th><th>P/L</th></tr>{rows}</table>
<small>¢ = avg profit per contract after fees, ± = rough 95% range. A strategy only has an edge if it beats
random_baseline and the whole range stays above 0 after {MIN_SETTLED_FOR_VERDICT}+ trades.</small></div>
<div class=card><b>Recent trades</b><table>{trs}</table></div>
<small><a style="color:#8b949e" href="/trades.csv">trades.csv</a> · <a style="color:#8b949e" href="/snapshots.csv">snapshots.csv</a></small>
</body></html>"""


def csv_dump(db, table):
    cur_rows = db.q(f"SELECT * FROM {table}")
    cols = [d[1] for d in db.q(f"PRAGMA table_info({table})")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(cur_rows)
    return buf.getvalue()


def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        def send(self, body, ctype="text/html; charset=utf-8"):
            b = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            try:
                if self.path.startswith("/api/stats"):
                    self.send(json.dumps({"stats": strategy_stats(engine.db),
                                          "status": {k: v for k, v in engine.status.items()}}, default=str),
                              "application/json")
                elif self.path.startswith("/trades.csv"):
                    self.send(csv_dump(engine.db, "trades"), "text/csv")
                elif self.path.startswith("/snapshots.csv"):
                    self.send(csv_dump(engine.db, "snapshots"), "text/csv")
                elif self.path.startswith("/health"):
                    self.send("ok", "text/plain")
                else:
                    self.send(render_dashboard(engine))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, *a):
            pass
    return H


def main():
    engine = Engine(DB(DB_PATH))
    threading.Thread(target=engine.run, daemon=True).start()
    print(f"Paper bot running. DB={DB_PATH}. Dashboard on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(engine)).serve_forever()


if __name__ == "__main__":
    main()
