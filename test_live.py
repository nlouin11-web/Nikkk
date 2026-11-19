import base64, importlib, os, tempfile, time, unittest
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
import main as M
import live as L


class FakeClient:
    ready = True

    def __init__(self):
        self.orders = []

    def buy_ioc(self, ticker, side, price, count):
        self.orders.append((ticker, side, price, count))
        return {"order_id": "o1", "status": "filled", "fill_count": count,
                "cost": price * count, "fees": 0.02}


def state(ticker, ask=0.60, close=None, p_yes=0.85, min_left=5):
    return {"ticker": ticker, "close_ts": close or time.time() + 300, "ask": {"yes": ask, "no": 1 - ask},
            "p_yes": p_yes, "min_left": min_left}


class T(unittest.TestCase):
    def setUp(self):
        self.db = M.DB(tempfile.mktemp(suffix=".db"))
        self.c = FakeClient()
        self.lt = L.LiveTrader(self.db, "x", M.strategy_stats, client=self.c)

    def test_auto_blocks_unproven(self):
        L.LIVE_MODE = "auto"
        self.assertIsNone(self.lt.maybe_trade(state("A"), "yes", time.time(), M.taker_fee))
        self.assertEqual(self.c.orders, [])
        self.assertIn("Need more data", self.lt.status()[1])

    def test_auto_allows_proven(self):
        L.LIVE_MODE = "auto"
        # 300 settled winning fair_value trades, losing baseline
        for i in range(300):
            self.db.q("INSERT INTO trades(ts,strategy,ticker,close_ts,side,price,contracts,fee,result,pnl)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)", (0, "fair_value", f"T{i}", 0, "yes", 0.5, 10, 0.18,
                                                      "yes" if i % 3 else "no", 4.82 if i % 3 else -5.18))
            self.db.q("INSERT INTO trades(ts,strategy,ticker,close_ts,side,price,contracts,fee,result,pnl)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)", (0, "random_baseline", f"T{i}", 0, "yes", 0.5, 10, 0.18,
                                                      "no", -5.18))
        self.assertTrue(self.lt.status()[0])
        self.assertIsNotNone(self.lt.maybe_trade(state("B"), "yes", time.time(), M.taker_fee))
        self.assertEqual(len(self.c.orders), 1)
        # one per window
        self.lt.maybe_trade(state("B"), "yes", time.time(), M.taker_fee)
        self.assertEqual(len(self.c.orders), 1)

    def test_daily_cap_and_kill(self):
        L.LIVE_MODE, L.DAILY_LOSS_CAP = "on", 5.0
        now = time.time()
        n = 0
        for i in range(20):
            if self.lt.maybe_trade(state(f"W{i}", ask=0.60), "yes", now, M.taker_fee):
                n += 1
        # each trade risks 0.60 + 0.02 fee = 0.62 -> 8 trades = 4.96 <= 5
        self.assertEqual(n, 8)
        self.assertIn("cap", self.lt.last_error)
        self.lt.set_killed(True)
        self.assertFalse(self.lt.status()[0])
        L.LIVE_MODE = "auto"

    def test_settle(self):
        L.LIVE_MODE = "on"
        past = time.time() - 60
        self.lt.maybe_trade(state("S", ask=0.40, close=past, p_yes=0.60), "yes", past - 100, M.taker_fee)
        self.lt.settle(time.time(), lambda t: {"result": "yes"})
        pnl = self.db.q("SELECT pnl FROM live_trades WHERE ticker='S'")[0][0]
        self.assertAlmostEqual(pnl, 1 - 0.40 - 0.02)
        L.LIVE_MODE = "auto"

    def test_signing_rsa_and_ed25519(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        for key in (rsa.generate_private_key(public_exponent=65537, key_size=2048),
                    ed25519.Ed25519PrivateKey.generate()):
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
            os.environ["KALSHI_PRIVATE_KEY"] = pem.replace("\n", "\\n")  # as pasted in one line
            k = L._load_key()
            h = L.Kalshi("https://api.elections.kalshi.com/trade-api/v2", k, "kid")._sign(
                "POST", "/trade-api/v2/portfolio/orders?x=1")
            msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "POST/trade-api/v2/portfolio/orders").encode()
            sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
            if isinstance(key, rsa.RSAPrivateKey):
                key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=32), hashes.SHA256())
            else:
                key.public_key().verify(sig, msg)

    def test_safety_filters(self):
        L.LIVE_MODE = "on"
        now = time.time()
        f = M.taker_fee
        self.assertIsNone(self.lt.maybe_trade(state("P1", ask=0.90, p_yes=0.99), "yes", now, f))  # too pricey
        self.assertIsNone(self.lt.maybe_trade(state("P2", ask=0.10, p_yes=0.40), "yes", now, f))  # lottery
        self.assertIsNone(self.lt.maybe_trade(state("P3", ask=0.60, p_yes=0.65), "yes", now, f))  # edge too small
        self.assertIsNone(self.lt.maybe_trade(state("P4", ask=0.60, p_yes=0.90, min_left=1), "yes", now, f))  # too late
        self.assertIsNone(self.lt.maybe_trade(state("P5", ask=0.60, p_yes=None), "yes", now, f))  # no model
        self.assertIsNotNone(self.lt.maybe_trade(state("P6", ask=0.60, p_yes=0.75), "yes", now, f))  # 15c edge -> ok
        self.assertIsNotNone(self.lt.maybe_trade(state("P7", ask=0.50, p_yes=0.30), "no", now, f))   # NO side, prob .70
        self.assertEqual(len(self.c.orders), 2)
        L.LIVE_MODE = "auto"

    def test_v2_order_body_and_parsing(self):
        sent = {}
        k = L.Kalshi("https://x/trade-api/v2", key=None, key_id="kid")
        def fake_request(method, ep, body=None):
            sent.update(method=method, ep=ep, body=body)
            return {"order_id": "abc", "fill_count": "1.00", "remaining_count": "0.00",
                    "average_fill_price": "0.5400", "average_fee_paid": "0.0200", "ts_ms": 1}
        k.request = fake_request
        o = k.buy_ioc("KXBTC15M-X", "no", 0.46, 1)   # buy NO at 46c == sell YES at 54c
        self.assertEqual(sent["ep"], "/portfolio/events/orders")
        self.assertEqual(sent["body"]["side"], "ask")
        self.assertEqual(sent["body"]["price"], "0.5400")
        self.assertEqual(sent["body"]["count"], "1")
        self.assertEqual(sent["body"]["time_in_force"], "immediate_or_cancel")
        self.assertAlmostEqual(o["cost"], 0.46)
        self.assertAlmostEqual(o["fees"], 0.02)
        o = k.buy_ioc("KXBTC15M-X", "yes", 0.55, 1)
        self.assertEqual(sent["body"]["side"], "bid")
        self.assertEqual(sent["body"]["price"], "0.5500")
        self.assertAlmostEqual(o["cost"], 0.54)  # filled at avg 0.54 YES

    def test_test_trade(self):
        self.c.key_id = "kid-123"
        st = state("TT", ask=0.70)  # yes .70, no .30 -> buys NO
        self.assertEqual(self.lt.test_trade(st, time.time(), M.taker_fee, "wrong"), "Wrong Key ID")
        msg = self.lt.test_trade(st, time.time(), M.taker_fee, "kid-123")
        self.assertIn("FILLED", msg)
        self.assertEqual(self.c.orders[-1][1], "no")
        self.assertIn("Already traded", self.lt.test_trade(st, time.time(), M.taker_fee, "kid-123"))

    def test_dashboard_renders_live_card(self):
        e = M.Engine(self.db, self.lt)
        page = M.render_dashboard(e)
        self.assertIn("KILL SWITCH", page)


if __name__ == "__main__":
    unittest.main()
