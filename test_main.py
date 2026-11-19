import math, os, tempfile, unittest
from datetime import datetime, timezone
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
import main as M


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class T(unittest.TestCase):
    def test_fee(self):
        self.assertEqual(M.taker_fee(1, 0.50), 0.02)   # 1.75c -> 2c
        self.assertEqual(M.taker_fee(10, 0.50), 0.18)  # 17.5c -> 18c
        self.assertEqual(M.taker_fee(100, 0.50), 1.75)

    def test_model(self):
        self.assertAlmostEqual(M.fair_yes_prob(100, 100, 10, 0.001), 0.5)
        self.assertGreater(M.fair_yes_prob(100.3, 100, 2, 0.001), 0.9)
        self.assertIsNone(M.fair_yes_prob(100, None, 5, 0.001))

    def test_full_cycle(self):
        db = M.DB(tempfile.mktemp(suffix=".db"))
        e = M.Engine(db)
        t0 = 1_800_000_000 - (1_800_000_000 % 900)
        # warm up 70 minutes of spot so vol + momentum work
        import random
        random.seed(1)
        price = 90000.0
        for i in range(70):
            price *= 1 + random.gauss(0, 0.0008)
            e.vol.add(t0 - (70 - i) * 60, price)
        close = t0 + 900
        mk = {"ticker": "KXBTC15M-TEST-00", "open_time": iso(t0), "close_time": iso(close),
              "yes_bid_dollars": "0.5000", "yes_ask_dollars": "0.5200",
              "no_bid_dollars": "0.4800", "no_ask_dollars": "0.5000", "floor_strike": price}
        spot = {"v": price}
        # walk through the window every 5s, BTC drifting up hard late
        for s in range(0, 900, 5):
            now = t0 + s
            if s > 300:
                spot["v"] = price * 1.004
                mk.update(yes_ask_dollars="0.6000", yes_bid_dollars="0.5800",
                          no_ask_dollars="0.4200", no_bid_dollars="0.4000")
            e.step(now=now, market_fn=lambda n: mk, spot_fn=lambda: spot["v"], fetch=lambda t: {})
        trades = db.q("SELECT strategy, side, price FROM trades")
        names = {t[0] for t in trades}
        self.assertIn("random_baseline", names)
        self.assertIn("momentum_open", names)
        self.assertIn("leader_8min", names)
        self.assertIn("fair_value", names)  # model ~0.99 vs ask 0.60 -> buys YES
        self.assertEqual(len(trades), len(names))  # one per strategy per window
        # settle: YES wins
        e.settle(close + 10, fetch=lambda t: {"result": "yes"})
        for strat, side, price_, pnl, fee in db.q("SELECT strategy, side, price, pnl, fee FROM trades"):
            exp = (10 if side == "yes" else 0) - 10 * price_ - fee
            self.assertAlmostEqual(pnl, exp, places=6)
        stats = M.strategy_stats(db)
        self.assertEqual(sum(s["settled"] for s in stats), len(trades))
        page = M.render_dashboard(e)
        self.assertIn("fair_value", page)
        self.assertIn("strategy,ticker", M.csv_dump(db, "trades"))


if __name__ == "__main__":
    unittest.main()
