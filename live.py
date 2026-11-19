"""
Live trading for the BTC 15-min bot. REAL MONEY.

Safety rails (all enforced here, before any order is sent):
  - LIVE_MODE=auto (default): only trades once LIVE_STRATEGY's paper results say
    "EDGE LOOKS REAL" AND it beats random_baseline. off = never. on = force.
  - Needs KALSHI_KEY_ID + KALSHI_PRIVATE_KEY in the environment (Railway Variables).
  - LIVE_CONTRACTS per trade (default 1), one trade per window.
  - DAILY_LOSS_CAP (default $5): counts today's realized losses PLUS the full cost
    of open live positions, so the cap holds even if every open trade loses.
  - Kill switch on the dashboard stops all new live orders immediately.
  - Orders are immediate-or-cancel limit orders at the current ask: no resting
    orders, never pays more than the price the strategy saw.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

LIVE_MODE = os.environ.get("LIVE_MODE", "auto").lower()          # off | auto | on
LIVE_STRATEGY = os.environ.get("LIVE_STRATEGY", "fair_value")
LIVE_CONTRACTS = int(os.environ.get("LIVE_CONTRACTS", "1"))
DAILY_LOSS_CAP = float(os.environ.get("DAILY_LOSS_CAP", "5"))
KEY_ID = os.environ.get("KALSHI_KEY_ID", "").strip()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=-8))


def _load_key():
    pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not pem:
        return None
    pem = pem.replace("\\n", "\n")
    if "BEGIN" not in pem:          # allow the PEM pasted as base64 of the whole file
        try:
            pem = base64.b64decode(pem).decode()
        except Exception:
            return None
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(pem.encode(), password=None)


class Kalshi:
    """Signed requests to Kalshi's trading API."""

    def __init__(self, base, key=None, key_id=KEY_ID):
        self.base = base.rstrip("/")
        self.key = key
        self.key_id = key_id

    @property
    def ready(self):
        return bool(self.key and self.key_id)

    def _sign(self, method, path):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path.split("?")[0]).encode()
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            sig = self.key.sign(msg)
        elif isinstance(self.key, rsa.RSAPrivateKey):
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        else:
            raise ValueError("Unsupported key type")
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def request(self, method, endpoint, body=None):
        url = self.base + endpoint
        path = urllib.request.urlparse(url).path
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "btc15-live/1.0", **self._sign(method, path)}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Kalshi {e.code}: {e.read().decode()[:300]}")

    def balance(self):
        return self.request("GET", "/portfolio/balance")

    def buy_ioc(self, ticker, side, price, count):
        price_cents = int(round(price * 100))
        body = {"ticker": ticker, "side": side, "action": "buy", "count": count, "type": "limit",
                "time_in_force": "immediate_or_cancel", "client_order_id": str(uuid.uuid4()),
                f"{side}_price": price_cents}
        return self.request("POST", "/portfolio/orders", body).get("order", {})


def _cents_or_dollars(order, name):
    d = order.get(f"{name}_dollars")
    if d not in (None, ""):
        return float(d)
    v = order.get(name)
    return (v or 0) / 100


class LiveTrader:
    def __init__(self, db, base, stats_fn, client=None):
        self.db = db
        self.stats_fn = stats_fn
        self.last_error = None
        self.last_balance = None
        if client is None:
            key = None
            try:
                key = _load_key()
            except Exception as e:
                self.last_error = f"Key error: {e}"
            client = Kalshi(base, key)
        self.client = client
        db.q("""CREATE TABLE IF NOT EXISTS live_trades(
            id INTEGER PRIMARY KEY, ts REAL, ticker TEXT UNIQUE, close_ts REAL, side TEXT,
            limit_price REAL, filled INTEGER, cost REAL, fee REAL, order_id TEXT, status TEXT,
            result TEXT, pnl REAL)""")
        db.q("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")

    # ---- kill switch
    @property
    def killed(self):
        r = self.db.q("SELECT v FROM settings WHERE k='killed'")
        return bool(r and r[0][0] == "1")

    def set_killed(self, on):
        self.db.q("INSERT OR REPLACE INTO settings VALUES('killed', ?)", ("1" if on else "0",))

    # ---- gates
    def strategy_proven(self):
        stats = {s["strategy"]: s for s in self.stats_fn(self.db)}
        s, base = stats.get(LIVE_STRATEGY), stats.get("random_baseline")
        if not s:
            return False, f"Unknown strategy {LIVE_STRATEGY}"
        if s["verdict"] != "EDGE LOOKS REAL":
            return False, f"{LIVE_STRATEGY}: {s['verdict']}"
        if base and base["settled"] and s["cents_per_contract"] <= base["cents_per_contract"]:
            return False, f"{LIVE_STRATEGY} not beating random_baseline"
        return True, "proven"

    def status(self):
        if LIVE_MODE == "off":
            return False, "LIVE_MODE=off"
        if not self.client.ready:
            return False, self.last_error or "No Kalshi key (add KALSHI_KEY_ID + KALSHI_PRIVATE_KEY)"
        if self.killed:
            return False, "Kill switch ON"
        if LIVE_MODE == "on":
            return True, "LIVE_MODE=on (forced)"
        return self.strategy_proven()

    def today_start(self, now):
        d = datetime.fromtimestamp(now, LOCAL_TZ)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def today_pnl(self, now):
        r = self.db.q("SELECT COALESCE(SUM(pnl),0) FROM live_trades WHERE pnl IS NOT NULL AND ts >= ?",
                      (self.today_start(now),))
        return r[0][0]

    def at_risk(self, now):
        """Today's realized loss + everything still riding on open trades."""
        open_cost = self.db.q("SELECT COALESCE(SUM(cost+fee),0) FROM live_trades WHERE result IS NULL AND filled>0")[0][0]
        return max(0.0, -self.today_pnl(now)) + open_cost

    # ---- actions
    def maybe_trade(self, st, side, now, fee_fn):
        ok, why = self.status()
        if not ok:
            return None
        price = st["ask"][side]
        if not price or price >= 0.99:
            return None
        if self.db.q("SELECT 1 FROM live_trades WHERE ticker=?", (st["ticker"],)):
            return None
        worst = price * LIVE_CONTRACTS + fee_fn(LIVE_CONTRACTS, price)
        if self.at_risk(now) + worst > DAILY_LOSS_CAP + 1e-9:
            self.last_error = f"Daily loss cap ${DAILY_LOSS_CAP:.2f} reached, skipping"
            return None
        try:
            o = self.client.buy_ioc(st["ticker"], side, price, LIVE_CONTRACTS)
        except Exception as e:
            self.last_error = str(e)
            self.db.q("INSERT OR IGNORE INTO live_trades(ts,ticker,close_ts,side,limit_price,filled,cost,fee,status)"
                      " VALUES(?,?,?,?,?,0,0,0,'error')", (now, st["ticker"], st["close_ts"], side, price))
            return None
        filled = int(float(o.get("fill_count_fp") or o.get("fill_count") or 0))
        cost = _cents_or_dollars(o, "taker_fill_cost") + _cents_or_dollars(o, "maker_fill_cost")
        fee = _cents_or_dollars(o, "taker_fees") + _cents_or_dollars(o, "maker_fees")
        self.db.q("INSERT OR IGNORE INTO live_trades(ts,ticker,close_ts,side,limit_price,filled,cost,fee,order_id,status)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (now, st["ticker"], st["close_ts"], side, price, filled, cost, fee, o.get("order_id"), o.get("status")))
        self.last_error = None
        return o

    def settle(self, now, fetch):
        rows = self.db.q("SELECT id, ticker, side, filled, cost, fee FROM live_trades"
                         " WHERE result IS NULL AND close_ts < ?", (now - 3,))
        for tid, ticker, side, filled, cost, fee in rows:
            if not filled:
                self.db.q("UPDATE live_trades SET result='unfilled', pnl=0 WHERE id=?", (tid,))
                continue
            res = (fetch(ticker).get("result") or "").lower()
            if res in ("yes", "no"):
                pnl = (filled if res == side else 0) - cost - fee
                self.db.q("UPDATE live_trades SET result=?, pnl=? WHERE id=?", (res, pnl, tid))

    def summary(self, now):
        ok, why = self.status()
        tot = self.db.q("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM live_trades WHERE filled>0")[0]
        return {"enabled": ok, "reason": why, "mode": LIVE_MODE, "strategy": LIVE_STRATEGY,
                "contracts": LIVE_CONTRACTS, "cap": DAILY_LOSS_CAP, "killed": self.killed,
                "today_pnl": self.today_pnl(now), "at_risk": self.at_risk(now),
                "trades": tot[0], "total_pnl": tot[1], "error": self.last_error,
                "can_resume": bool(ADMIN_TOKEN)}
