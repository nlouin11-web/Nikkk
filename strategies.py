"""
Strategy library: every published approach for 15-minute BTC up/down markets that can be tested with
Kalshi 1-minute candles + BTC 1-minute prices. Each strategy looks at the window minute by minute
(ctx list, oldest first) and returns (side, ctx) for its first trade, or None. Held to settlement.

Sources (Nov 2026 research): Simple Kalshi Bot (PREVIOUS, MOMENTUM, CONSENSUS, MOMENTUM_15, PREVIOUS_2),
Kalshibot (spot divergence), latency/momentum-lag arbitrage write-ups, quant research notes
(10%+ edge rule, minute 4-7 entry window, mean reversion), botforkalshi studies (leader, 90-99c favorites).

Not testable here (no history available): Dutch-book YES+NO<$1 arbitrage (impossible on Kalshi's single
book), Deribit options-implied pricing, order-book imbalance, cross-venue Polymarket lag.
"""


def _ask(c, side):
    return c["ya"] if side == "yes" else (1 - c["yb"] if c["yb"] else None)


def _ok(c, side, lo=0.05, hi=0.95):
    a = _ask(c, side)
    return a is not None and lo <= a <= hi


def first(ctx, cond, lo_min=0.0, hi_min=15.0):
    for c in ctx:
        if lo_min <= c["mins"] <= hi_min:
            side = cond(c)
            if side and _ok(c, side):
                return side, c
    return None


def fair_edge(min_edge, lo_min=2, hi_min=10, lo_p=0.15, hi_p=0.85):
    def f(ctx, prev, fee):
        def cond(c):
            if c["p"] is None:
                return None
            for side, prob in (("yes", c["p"]), ("no", 1 - c["p"])):
                a = _ask(c, side)
                if a and lo_p <= a <= hi_p and prob - a - fee(1, a) >= min_edge:
                    return side
        return first(ctx, cond, lo_min, hi_min)
    return f


def previous(max_price=0.95):
    def f(ctx, prev, fee):
        if prev not in ("yes", "no"):
            return None
        return first(ctx, lambda c: prev if (_ask(c, prev) or 1) <= max_price else None, 12, 14.5)
    return f


def momentum(lookback, lo_min=12, hi_min=14.5):
    def f(ctx, prev, fee):
        return first(ctx, lambda c: (None if c.get(f"r{lookback}") is None
                                     else ("yes" if c[f"r{lookback}"] > 0 else "no")), lo_min, hi_min)
    return f


def consensus(ctx, prev, fee):
    def cond(c):
        r = c.get("r1")
        if prev not in ("yes", "no") or r is None:
            return None
        mom = "yes" if r > 0 else "no"
        return prev if mom == prev else None
    return first(ctx, cond, 12, 14.5)


def leader(at_min):
    def f(ctx, prev, fee):
        return first(ctx, lambda c: None if c["mid"] is None else ("yes" if c["mid"] >= 0.5 else "no"),
                     at_min - 0.5, at_min + 0.5)
    return f


def latency_lag(min_move=0.0015, max_mid_move=0.05):
    """Spot moved hard in the last 2 minutes but the market barely repriced -> buy the spot's direction."""
    def f(ctx, prev, fee):
        def cond(c):
            r2, dm = c.get("r2"), c.get("dmid2")
            if r2 is None or dm is None or abs(r2) < min_move:
                return None
            side = "yes" if r2 > 0 else "no"
            moved_with = dm if side == "yes" else -dm
            return side if moved_with < max_mid_move else None
        return first(ctx, cond, 2, 12)
    return f


def mean_reversion(min_mid_jump=0.25, max_spot_move=0.0008):
    """Market price jumped a lot in 2 minutes while spot barely moved -> overreaction, fade it."""
    def f(ctx, prev, fee):
        def cond(c):
            r2, dm = c.get("r2"), c.get("dmid2")
            if r2 is None or dm is None or abs(dm) < min_mid_jump or abs(r2) > max_spot_move:
                return None
            return "no" if dm > 0 else "yes"
        return first(ctx, cond, 2, 12)
    return f


def late_favorite(ctx, prev, fee):
    def cond(c):
        for side in ("yes", "no"):
            a = _ask(c, side)
            if a and 0.90 <= a <= 0.97:
                return side
    return first(ctx, cond, 1, 3)


def value_previous(ctx, prev, fee):
    """PREVIOUS_2: follow last window's result, but only at a good price (<= 45c)."""
    if prev not in ("yes", "no"):
        return None
    return first(ctx, lambda c: prev if (_ask(c, prev) or 1) <= 0.45 else None, 3, 14.5)


def dump_lock(entry_max=0.35, lock_margin=0.02, min_left_entry=5):
    """Dump-and-hedge, Kalshi version. Buy a side once it dumps to <= 35c. On Kalshi, 'hedging' with the
    other side nets out the position, i.e. selling it back: lock in as soon as the bid covers entry +
    both fees + margin. Otherwise hold to settlement. Returns a custom result with its own pnl."""
    def f(ctx, prev, fee, result):
        entry = None
        for c in ctx:
            if entry is None:
                if c["mins"] < min_left_entry:
                    return None
                for side in ("yes", "no"):
                    a = _ask(c, side)
                    if a and 0.05 <= a <= entry_max:
                        entry = (side, a, a + fee(1, a))
                        break
                continue
            side, a, cost = entry
            bid = c["yb"] if side == "yes" else (1 - c["ya"] if c["ya"] else None)
            if bid and bid - fee(1, bid) - cost >= lock_margin:
                return {"side": side, "price": a, "pnl": bid - fee(1, bid) - cost, "won": 1, "locked": True}
        if not entry:
            return None
        side, a, cost = entry
        return {"side": side, "price": a, "pnl": (1 if result == side else 0) - cost,
                "won": 1 if result == side else 0, "locked": False}
    f.custom = True
    return f


def early_favorite(lo=0.85, hi=0.95):
    """Published rule (out-of-sample positive on Polymarket): 10-15 min left, buy the favorite at 85-95c."""
    def f(ctx, prev, fee):
        def cond(c):
            for side in ("yes", "no"):
                a = _ask(c, side)
                if a and lo <= a <= hi:
                    return side
        return first(ctx, cond, 10, 15)
    return f


def late_momentum(min_move=0.002):
    """Published rule: minutes 8-12 (3-7 left), buy the side BTC has moved >0.20% toward since the open."""
    def f(ctx, prev, fee):
        def cond(c):
            if not ctx or not ctx[0]["spot"] or not c["spot"]:
                return None
            move = c["spot"] / ctx[0]["spot"] - 1
            if abs(move) < min_move:
                return None
            return "yes" if move > 0 else "no"
        return first(ctx, cond, 3, 7)
    return f


LIBRARY = {
    "fair_value_5c (current live)": fair_edge(0.05),
    "fair_value_10c": fair_edge(0.10),
    "fair_value_15c": fair_edge(0.15),
    "fair_value_min4-7": fair_edge(0.05, 8, 11),
    "previous": previous(),
    "value_previous (<=45c)": value_previous,
    "momentum_1m": momentum(1),
    "momentum_15m": momentum(15),
    "consensus": consensus,
    "leader_12m (your rule)": leader(12),
    "leader_8m": leader(8),
    "leader_4m": leader(4),
    "latency_lag": latency_lag(),
    "mean_reversion": mean_reversion(),
    "late_favorite_90-97c": late_favorite,
    "dump_and_lock (<=35c, lock +2c)": dump_lock(),
    "early_favorite_85-95c (10-15m)": early_favorite(),
    "late_momentum_0.20%": late_momentum(),
}
