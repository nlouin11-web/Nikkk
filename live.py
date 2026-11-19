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
# "Safer trade" filters for real money (stricter than paper):
LIVE_MIN_EDGE = float(os.environ.get("LIVE_MIN_EDGE", "0.05"))   # model must beat price+fee by 5c
LIVE_MIN_PRICE = float(os.environ.get("LIVE_MIN_PRICE", "0.15"))  # no lottery tickets
LIVE_MAX_PRICE = float(os.environ.get("LIVE_MAX_PRICE", "0.85"))  # no 95c bets where one loss wipes 20 wins
BALANCE_FLOOR = float(os.environ.get("BALANCE_FLOOR", "6"))  # auto kill switch if Kalshi cash drops below this
TAKE_PROFIT = float(os.environ.get("TAKE_PROFIT", "0.03"))  # sell once locked-in profit >= 3c/contract after all fees
LIVE_MIN_MINUTES = float(os.environ.get("LIVE_MIN_MINUTES", "2"))  # skip the chaotic last 2 minutes
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
        """Kalshi V2 orders are quoted on the YES leg: 'bid' buys YES, 'ask' sells YES.
        Buying NO at p == selling YES at (1 - p). Returns a normalized dict in the side's terms."""
        yes_price = price if side == "yes" else 1 - price
        body = {"ticker": ticker, "side": "bid" if side == "yes" else "ask",
                "count": str(int(count)), "price": f"{yes_price:.4f}",
                "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": str(uuid.uuid4()), "post_only": False, "reduce_only": False}
        r = self.request("POST", "/portfolio/events/orders", body)
        filled = float(r.get("fill_count") or 0)
        avg_yes = float(r.get("average_fill_price") or yes_price) if filled else 0.0
        per = avg_yes if side == "yes" else 1 - avg_yes
        return {"order_id": r.get("order_id"), "status": "filled" if filled else "unfilled",
                "fill_count": filled, "cost": round(per * filled, 4),
                "fees": round(float(r.get("average_fee_paid") or 0) * filled, 4)}

    def sell_ioc(self, ticker, side, price, count):
        r = self.request("POST", "/portfolio/events/orders", _sell_body(ticker, side, price, count))
        filled = float(r.get("fill_count") or 0)
        yes_price = price if side == "yes" else 1 - price
        avg_yes = float(r.get("average_fill_price") or yes_price) if filled else 0.0
        per = avg_yes if side == "yes" else 1 - avg_yes
        return {"fill_count": filled, "proceeds": round(per * filled, 4),
                "fees": round(float(r.get("average_fee_paid") or 0) * filled, 4)}


def _parse_any_ts(v):
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f / 1000 if f > 1e11 else f
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None


def _sell_body(ticker, side, price, count):
    """Close a position: selling YES = 'ask' at p; selling NO = buying YES leg at (1-p). reduce_only so it
    can never open a new position."""
    yes_price = price if side == "yes" else 1 - price
    return {"ticker": ticker, "side": "ask" if side == "yes" else "bid", "count": str(int(count)),
            "price": f"{yes_price:.4f}", "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross", "client_order_id": str(uuid.uuid4()),
            "post_only": False, "reduce_only": True}


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
        if not price or not (LIVE_MIN_PRICE <= price <= LIVE_MAX_PRICE):
            return None
        p_yes = st.get("p_yes")
        if p_yes is None or st.get("min_left", 99) < LIVE_MIN_MINUTES:
            return None
        prob = p_yes if side == "yes" else 1 - p_yes
        if prob - price - fee_fn(1, price) < LIVE_MIN_EDGE:
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
        filled = int(float(o.get("fill_count") or 0))
        cost, fee = float(o.get("cost") or 0), float(o.get("fees") or 0)
        self.db.q("INSERT OR IGNORE INTO live_trades(ts,ticker,close_ts,side,limit_price,filled,cost,fee,order_id,status)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (now, st["ticker"], st["close_ts"], side, price, filled, cost, fee, o.get("order_id"), o.get("status")))
        self.last_error = None
        return o

    def test_trade(self, st, now, fee_fn, key_id):
        """One manual test order: 1 contract on the cheaper side. Needs the Kalshi Key ID.
        Respects the kill switch and the daily loss cap; skips filters."""
        if not (self.client.ready and key_id and key_id.strip() == self.client.key_id):
            return "Wrong Key ID"
        if self.killed:
            return "Kill switch is ON"
        if not st:
            return "No open window right now"
        asks = {k: v for k, v in st["ask"].items() if v and 0.02 <= v <= 0.60}
        if not asks:
            return "No cheap side to test with right now, try again in a minute"
        side = min(asks, key=asks.get)
        price = asks[side]
        if self.at_risk(now) + price + fee_fn(1, price) > DAILY_LOSS_CAP + 1e-9:
            return "Daily loss cap reached"
        if self.db.q("SELECT 1 FROM live_trades WHERE ticker=?", (st["ticker"],)):
            return "Already traded this window, try after the next one opens"
        try:
            o = self.client.buy_ioc(st["ticker"], side, price, 1)
        except Exception as e:
            self.last_error = str(e)
            return f"Kalshi error: {e}"
        filled = int(float(o.get("fill_count") or 0))
        self.db.q("INSERT OR IGNORE INTO live_trades(ts,ticker,close_ts,side,limit_price,filled,cost,fee,order_id,status)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (now, st["ticker"], st["close_ts"], side, price, filled, float(o.get("cost") or 0),
                   float(o.get("fees") or 0), o.get("order_id"), "test"))
        self.last_error = None
        if not filled:
            return f"Order sent but not filled (price moved). Try again next window."
        return f"FILLED: bought 1 {side.upper()} at {price:.2f} on {st['ticker']}"

    def check_balance(self, now):
        """Every minute: read Kalshi cash. Below BALANCE_FLOOR -> flip the kill switch automatically."""
        if not self.client.ready or now - getattr(self, "_bal_ts", 0) < 60:
            return
        self._bal_ts = now
        try:
            r = self.client.request("GET", "/portfolio/balance")
        except Exception as e:
            self.last_error = f"balance: {e}"
            return
        bal = r.get("balance_dollars")
        bal = float(bal) if bal not in (None, "") else (r.get("balance") or 0) / 100
        self.last_balance = bal
        if bal < BALANCE_FLOOR and not self.killed:
            self.set_killed(True)
            self.last_error = f"AUTO-STOP: balance ${bal:.2f} fell below ${BALANCE_FLOOR:.2f} floor"

    def resync(self, get_market, series="KXBTC15M"):
        """Rebuild live state from Kalshi after a restart: open positions (so take-profit and settlement keep
        working) and today's settled results (so the daily loss cap can't reset by restarting)."""
        if not self.client.ready:
            return 0
        n = 0
        for status in ("unsettled", "settled"):
            cursor = ""
            for _ in range(10):
                q = f"/portfolio/positions?count_filter=total_traded&settlement_status={status}&limit=200"
                r = self.client.request("GET", q + (f"&cursor={cursor}" if cursor else ""))
                for p in r.get("market_positions", []):
                    tk = p.get("ticker", "")
                    if not tk.startswith(series) or self.db.q("SELECT 1 FROM live_trades WHERE ticker=?", (tk,)):
                        continue
                    pos = float(p.get("position_fp") or 0)
                    fees = float(p.get("fees_paid_dollars") or 0)
                    ts = _parse_any_ts(p.get("last_updated_ts")) or time.time()
                    if status == "unsettled" and pos:
                        try:
                            close_ts = datetime.fromisoformat(get_market(tk)["close_time"].replace("Z", "+00:00")).timestamp()
                        except Exception:
                            close_ts = time.time() + 900
                        self.db.q("INSERT OR IGNORE INTO live_trades(ts,ticker,close_ts,side,limit_price,filled,cost,fee,status)"
                                  " VALUES(?,?,?,?,?,?,?,?,'synced')",
                                  (ts, tk, close_ts, "yes" if pos > 0 else "no", 0, int(abs(pos)),
                                   float(p.get("market_exposure_dollars") or 0), fees))
                        n += 1
                    elif status == "settled" and ts >= self.today_start(time.time()):
                        pnl = float(p.get("realized_pnl_dollars") or 0) - fees   # conservative: count fees as lost
                        self.db.q("INSERT OR IGNORE INTO live_trades(ts,ticker,close_ts,side,limit_price,filled,cost,fee,"
                                  "status,result,pnl) VALUES(?,?,?,?,0,0,0,0,'synced','settled',?)",
                                  (ts, tk, ts, "yes", pnl))
                        n += 1
                cursor = r.get("cursor") or ""
                if not cursor:
                    break
        return n

    def take_profit(self, st, now, fee_fn):
        """Sell open live positions in the current window as soon as the bid locks in a profit."""
        if not st or not self.client.ready:
            return
        bids = {"yes": st.get("yes_bid"), "no": st.get("no_bid")}
        rows = self.db.q("SELECT id, side, filled, cost, fee FROM live_trades WHERE ticker=? AND result IS NULL"
                         " AND filled>0", (st["ticker"],))
        for tid, side, filled, cost, fee in rows:
            bid = bids.get(side)
            if not bid:
                continue
            profit = bid * filled - fee_fn(filled, bid) - cost - fee
            if profit < TAKE_PROFIT * filled:
                continue
            try:
                o = self.client.sell_ioc(st["ticker"], side, bid, filled)
            except Exception as e:
                self.last_error = f"Sell failed: {e}"
                continue
            sold = int(float(o.get("fill_count") or 0))
            if sold >= filled:
                pnl = float(o["proceeds"]) - float(o["fees"]) - cost - fee
                self.db.q("UPDATE live_trades SET result='sold', pnl=? WHERE id=?", (pnl, tid))
                self.last_error = None

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
        return {"balance": self.last_balance, "floor": BALANCE_FLOOR, "enabled": ok, "reason": why, "mode": LIVE_MODE, "strategy": LIVE_STRATEGY,
                "contracts": LIVE_CONTRACTS, "cap": DAILY_LOSS_CAP, "killed": self.killed,
                "today_pnl": self.today_pnl(now), "at_risk": self.at_risk(now),
                "trades": tot[0], "total_pnl": tot[1], "error": self.last_error,
                "can_resume": bool(ADMIN_TOKEN)}
