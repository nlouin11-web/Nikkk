import math, os, tempfile, time, unittest
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
import main as M
import backtest as B
import live as L
from datetime import datetime, timezone


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class T(unittest.TestCase):
    def test_window_and_run(self):
        import random
        random.seed(3)
        now = int(time.time()) // 900 * 900
        o, c = now - 900 * 3, now - 900 * 2
        btc, p = {}, 60000.0
        for t in range(o - 3 * 3600, now, 60):
            p *= 1 + random.gauss(0, 0.0006)
            btc[t] = p
        strike = btc[o - 60]
        # BTC jumps +0.4% at minute 6 so the model says YES ~0.99 while ask lags at 0.60
        for t in range(o + 360, now, 60):
            btc[t] = btc[t] * 1.004 if t < c else btc[t]
        m = {"ticker": "KXBTC15M-BT1", "open_time": iso(o), "close_time": iso(c),
             "floor_strike": strike, "result": "yes"}
        candles = [{"end_period_ts": t, "yes_ask": {"close_dollars": "0.6000"},
                    "yes_bid": {"close_dollars": "0.5800"}} for t in range(o + 60, c + 1, 60)]
        tr = B.simulate_window(m, candles, btc, M.fair_yes_prob, M.taker_fee)
        self.assertIsNotNone(tr)
        self.assertEqual(tr["side"], "yes")
        self.assertAlmostEqual(tr["pnl"], 1 - 0.60 - M.taker_fee(1, 0.60))
        self.assertGreaterEqual(tr["minutes_left"], 2)

        def http(url):
            if "coinbase" in url:
                return [[t, 0, 0, 0, v, 0] for t, v in btc.items()]
            if "/candlesticks" in url:
                return {"candlesticks": candles}
            return {"markets": [m, dict(m, ticker="KXBTC15M-OPEN", result=""), dict(m, ticker="OTHERMKT-1")]}
        db = M.DB(tempfile.mktemp(suffix=".db"))
        st = {}
        B.time.sleep = lambda s: None
        s = B.run(db, http, "x", M.fair_yes_prob, M.taker_fee, status=st)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["windows"], 1)          # unsettled market skipped
        self.assertEqual(s["n"], 1)
        self.assertFalse(s["edge"])                 # < 300 trades never counts as edge

    def test_trend12(self):
        c = 1_800_000_000
        m = {"ticker": "KXBTC15M-T", "close_time": iso(c), "result": "yes"}
        candles = [{"end_period_ts": c - 12 * 60, "yes_ask": {"close_dollars": "0.6800"},
                    "yes_bid": {"close_dollars": "0.6600"}}]
        tr = B.simulate_trend12(m, candles, M.taker_fee)
        self.assertEqual(tr["side"], "yes")
        self.assertAlmostEqual(tr["pnl"], 1 - 0.68 - M.taker_fee(1, 0.68))
        m["result"] = "no"
        self.assertLess(B.simulate_trend12(m, candles, M.taker_fee)["pnl"], -0.68)

    def test_trend12_take_profit(self):
        c = 1_800_000_000
        m = {"ticker": "KXBTC15M-T", "close_time": iso(c), "result": "no"}
        cd = lambda mins, ya, yb: {"end_period_ts": c - mins * 60, "yes_ask": {"close_dollars": ya},
                                   "yes_bid": {"close_dollars": yb}}
        candles = [cd(12, "0.6200", "0.6000"), cd(10, "0.6600", "0.6400"), cd(8, "0.7400", "0.7200")]
        tr = B.simulate_trend12_tp(m, candles, M.taker_fee)
        self.assertEqual(tr["result"], "sold")        # bid .72 - fee - (.62+fee) >= .03 -> sold, even though NO won
        self.assertGreater(tr["pnl"], 0.03)
        flat = [cd(12, "0.6200", "0.6000"), cd(5, "0.6300", "0.6100")]
        tr2 = B.simulate_trend12_tp(m, flat, M.taker_fee)
        self.assertEqual(tr2["result"], "no")         # never hit target -> held, lost
        self.assertLess(tr2["pnl"], -0.6)

    def test_library_runs(self):
        import strategies as S
        c = 1_800_000_000
        o = c - 900
        btc = {t: 60000 + (t - o) / 60 * 10 for t in range(o - 7200, c + 60, 60)}   # steady uptrend
        m = {"ticker": "KXBTC15M-L", "open_time": iso(o), "close_time": iso(c), "floor_strike": 60000, "result": "yes"}
        candles = [{"end_period_ts": t, "yes_ask": {"close_dollars": "0.5500"}, "yes_bid": {"close_dollars": "0.5300"}}
                   for t in range(o + 60, c + 1, 60)]
        ctx = B.build_ctx(m, candles, btc, M.fair_yes_prob)
        self.assertTrue(ctx and ctx[0]["r1"] > 0)
        db = M.DB(tempfile.mktemp(suffix=".db"))
        db.q("CREATE TABLE backtest_multi(strategy TEXT, ticker TEXT, pnl REAL, won INTEGER, PRIMARY KEY(strategy, ticker))")
        B.run_library(db, m, ctx, "yes", M.taker_fee)
        names = {r[0] for r in db.q("SELECT strategy FROM backtest_multi")}
        self.assertIn("momentum_1m", names)
        self.assertIn("previous", names)
        self.assertIn("leader_12m (your rule)", names)
        lb = B.leaderboard(db)
        self.assertTrue(all("verdict" in r for r in lb))

    def test_dump_lock(self):
        import strategies as S
        mk = lambda mins, ya, yb: {"mins": mins, "ya": ya, "yb": yb}
        f = S.dump_lock()
        # YES dumps to .30, later bid recovers to .40 -> locked profit
        ctx = [mk(12, 0.50, 0.48), mk(10, 0.30, 0.28), mk(8, 0.36, 0.34), mk(6, 0.42, 0.40)]
        r = f(ctx, None, M.taker_fee, "no")
        self.assertTrue(r["locked"]); self.assertGreater(r["pnl"], 0.02)
        # never recovers, NO wins -> lose the 30c + fee
        ctx2 = [mk(12, 0.50, 0.48), mk(10, 0.30, 0.28), mk(5, 0.20, 0.18)]
        r2 = f(ctx2, None, M.taker_fee, "no")
        self.assertFalse(r2["locked"]); self.assertAlmostEqual(r2["pnl"], -(0.30 + M.taker_fee(1, 0.30)))

    def test_auto_gate_uses_backtest(self):
        db = M.DB(tempfile.mktemp(suffix=".db"))
        class C: ready = True
        lt = L.LiveTrader(db, "x", M.strategy_stats, client=C())
        db.q("CREATE TABLE IF NOT EXISTS backtest(ticker TEXT PRIMARY KEY, side TEXT, price REAL, fee REAL,"
             " minutes_left REAL, result TEXT, pnl REAL)")
        L.LIVE_MODE = "auto"
        for i in range(400):   # losing history
            db.q("INSERT INTO backtest VALUES(?,?,?,?,?,?,?)", (f"T{i}", "yes", .5, .02, 5, "no" if i % 2 else "yes",
                                                              -0.52 if i % 2 else 0.48))
        ok, why = lt.status()
        self.assertFalse(ok); self.assertIn("no proven edge", why)
        db.q("DELETE FROM backtest")
        for i in range(400):   # clearly winning history
            db.q("INSERT INTO backtest VALUES(?,?,?,?,?,?,?)", (f"T{i}", "yes", .5, .02, 5, "yes" if i % 3 else "no",
                                                              0.48 if i % 3 else -0.52))
        ok, why = lt.status()
        self.assertTrue(ok); self.assertIn("backtest edge", why)


if __name__ == "__main__":
    unittest.main()
