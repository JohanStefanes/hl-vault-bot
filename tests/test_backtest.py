"""Selbstcheck der Backtest-Logik. Laufen lassen:  python3 -m unittest discover tests"""
import json
import os
import tempfile
import unittest

from hlvault import backtest as bt, db, metrics as m, screener
from hlvault.config import CONFIG

DAY = bt.MS_DAY


def detail(returns, equity=100_000.0, start=0, step_days=10, commission=0.1,
           leader_fraction=0.1):
    """vaultDetails-Fake mit konstantem Kapital und vorgegebenen Renditen."""
    avh, pnl, cum = [[start, str(equity)]], [[start, "0"]], 0.0
    for i, r in enumerate(returns, 1):
        cum += r * equity
        t = start + i * step_days * DAY
        avh.append([t, str(equity)])
        pnl.append([t, str(cum)])
    block = {"accountValueHistory": avh, "pnlHistory": pnl}
    return {"portfolio": [["allTime", block]], "leaderCommission": commission,
            "leaderFraction": leader_fraction}


class PeriodReturn(unittest.TestCase):
    def test_inflow_in_same_bin_is_no_crash(self):
        # Echter Fall aus den Daten: $100 Basis, $556k Zufluss, -$12k PnL
        r = m.period_return(100.0, 556_456.0, 0.0, -12_115.0)
        self.assertAlmostEqual(r, -12_115.0 / (100.0 + 0.5 * 568_471.0))
        self.assertGreater(r, -0.05)

    def test_without_flows_is_plain_return(self):
        self.assertAlmostEqual(m.period_return(1000.0, 1100.0, 0.0, 100.0), 0.10)

    def test_never_below_total_loss(self):
        self.assertEqual(m.period_return(1000.0, 0.0, 0.0, -5000.0), -1.0)

    def test_empty_vault_is_skipped(self):
        self.assertIsNone(m.period_return(0.5, 100.0, 0.0, 1.0))


class Windows(unittest.TestCase):
    def test_forward_return_only_counts_window(self):
        d = detail([0.5, 0.1, 0.1, 0.5])
        self.assertAlmostEqual(bt.forward_return(d, 10 * DAY, 30 * DAY), 1.1 * 1.1 - 1)

    def test_fee_only_on_profit(self):
        self.assertAlmostEqual(bt.net_of_fee(0.20, 0.1), 0.18)
        self.assertAlmostEqual(bt.net_of_fee(-0.20, 0.1), -0.20)

    def test_basket_drawdown(self):
        a = [(0, 1.0), (1, 2.0), (2, 1.0)]
        self.assertAlmostEqual(bt.basket_drawdown([a]), -0.5)
        # Der zweite Vault daempft den Einbruch des Korbs
        flat = [(0, 1.0), (2, 1.0)]
        self.assertAlmostEqual(bt.basket_drawdown([a, flat]), 1.0 / 1.5 - 1.0)

    def test_summarize(self):
        s = bt.summarize([0.1, -0.2, 0.3])
        self.assertEqual((s["worst"], s["best"], s["windows"]), (-0.2, 0.3, 3))
        self.assertLessEqual(s["ci90_low"], s["mean_fwd"])
        self.assertGreaterEqual(s["ci90_high"], s["mean_fwd"])


class WalkForward(unittest.TestCase):
    def setUp(self):
        self.cut = 300 * DAY
        # A gewinnt vor dem Stichtag, B erst danach
        self.details = {"0xa": detail([0.02] * 30 + [-0.01] * 6),
                        "0xb": detail([0.001] * 30 + [0.05] * 6)}

    def test_cache_apr_has_no_influence(self):
        """Das APR im Cache ist der heutige Stand und darf nichts bewegen."""
        meta_a = {"0xa": {"apr": 0.0}, "0xb": {"apr": 99.0}}
        meta_b = {"0xa": {"apr": 99.0}, "0xb": {"apr": 0.0}}
        a = bt.run_window(self.details, meta_a, self.cut, 60, k=1)
        b = bt.run_window(self.details, meta_b, self.cut, 60, k=1)
        self.assertEqual(a.strat, b.strat)
        # Naiv heisst: Rendite bis zum Stichtag, also A
        self.assertAlmostEqual(a.strat_gross["cagr_top_naive"],
                               bt.forward_return(self.details["0xa"], self.cut,
                                                 self.cut + 60 * DAY))

    def test_vault_too_young_at_cutoff_does_not_qualify(self):
        res = bt.score_at(self.details, {}, 60 * DAY)
        self.assertFalse(any(r.passed for r in res))

    def test_default_windows_do_not_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.json")
            with open(path, "w") as fh:
                json.dump({"details": self.details, "meta": {},
                           "fetched_at": 360 * DAY}, fh)
            out = bt.walk_forward(path, horizons=(60,), n_cutoffs=3)
        blk = out["horizons"]["60"]
        self.assertFalse(blk["overlapping"])
        self.assertEqual(blk["cutoffs"][1] - blk["cutoffs"][0], 60 * DAY)


class Universe(unittest.TestCase):
    @staticmethod
    def raw(tvl, pnl, closed=False, child=False, created=0):
        return {"apr": 0.1, "pnls": [["allTime", pnl]], "summary": {
            "vaultAddress": "0x1", "name": "x", "leader": "0x2", "tvl": str(tvl),
            "isClosed": closed, "createTimeMillis": created,
            "relationship": {"type": "child" if child else "normal"}}}

    def test_keeps_closed_blowups_and_drops_dust(self):
        pick = lambda *a, **k: screener.backtest_universe([self.raw(*a, **k)], 5000.0)
        self.assertEqual(len(pick(0, ["0", "-80000"], closed=True)), 1)
        self.assertEqual(len(pick(100_000, ["0"])), 1)
        self.assertEqual(len(pick(10, ["0", "-10"], closed=True)), 0)
        self.assertEqual(len(pick(100_000, ["0"], child=True)), 0)

    def test_list_snapshot_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(os.path.join(tmp, "t.db"))
            day = db.save_vault_list(conn, [self.raw(5, ["0"], closed=True)])
            self.assertEqual(db.save_vault_list(conn, [self.raw(7, ["0"])]), day)
            rows = db.load_vault_list(conn, day)
            self.assertEqual((len(rows), rows[0]["tvl"], rows[0]["closed"]),
                             (1, "7", False))
            self.assertIsNone(db.load_vault_list(conn, "1999-01-01"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
