"""BalanceGuard: per-lot FazerCards cost, hide/show decisions, hysteresis."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.database.db import Database  # noqa: E402
from pubg_uc_spark.services.balance_guard import BalanceGuard  # noqa: E402


class FakeFazer:
    def __init__(self, balance):
        self._balance = balance
        self.prices = {"60": 0.88, "325": 4.44, "8100": 88.99}

    def balance(self):
        return self._balance

    def lot_cost_usd(self, base_picks):
        total = 0.0
        for d, c in base_picks.items():
            if d not in self.prices:
                return None
            total += self.prices[d] * c
        return round(total, 4)


class FakeCardinal:
    """Records set_lot_active calls and reports lots as active."""
    def __init__(self):
        self.calls = []       # (lot_id, active)

        class _Acc:
            def __init__(self, outer):
                self.outer = outer

            def get_lot_fields(self, lot_id):
                class _LF:
                    active = True
                    fields = {"active": "on"}
                return _LF()

            def save_lot(self, lf):
                return True
        self.account = _Acc(self)


def _mk(tmp_path, balance, *, enabled=True, mode="monitor", margin_pct=10.0):
    c = Config()
    c.database_path = str(tmp_path / "g.db")
    c.delivery_provider = "fazercards"
    c.balance_guard_enabled = enabled
    c.balance_guard_mode = mode
    c.balance_guard_margin_pct = margin_pct
    c.lots = {
        "L60": LotConfig("L60", "60 UC", "60", {"60": 1}),
        "L8100": LotConfig("L8100", "8100 UC", "8100", {"8100": 1}),
        "L8425": LotConfig("L8425", "8425 UC", "8425", {"8100": 1, "325": 1}),  # combo
    }
    db = Database(c.database_path)
    notes = []
    g = BalanceGuard(c, db, FakeFazer(balance), FakeCardinal(),
                     notifier=lambda t: notes.append(t), async_mode=False)
    return c, db, g, notes


def test_cost_calc_including_combo(tmp_path):
    c, db, g, notes = _mk(tmp_path, 1000)
    costs = {e["lot_id"]: e["cost"] for e in g.check_once()["lots"]}
    assert costs["L60"] == 0.88
    assert costs["L8100"] == 88.99
    assert costs["L8425"] == round(88.99 + 4.44, 4)   # combo = both packs
    db.close()


def test_monitor_notifies_but_never_hides(tmp_path):
    c, db, g, notes = _mk(tmp_path, 9.0, mode="monitor")   # covers 60, not 8100/8425
    g.cardinal.calls = []
    r = g.check_once()
    assert r["hidden"] == 0 and r["shown"] == 0            # monitor never edits
    assert g.cardinal.account.save_lot                      # exists but unused
    assert any("не хватает" in n for n in notes)
    db.close()


def test_enforce_hides_unaffordable_and_restores(tmp_path):
    c, db, g, notes = _mk(tmp_path, 9.0, mode="enforce")    # only 60 affordable
    # capture save_lot(active) by wrapping funpay helper via cardinal account
    from pubg_uc_spark.funpay import orders as fo
    calls = []
    orig = fo.set_lot_active
    fo.set_lot_active = lambda card, lid, active: (calls.append((lid, active)) or True)
    try:
        r = g.check_once()
        hidden = {lid for lid, a in calls if a is False}
        assert "L8100" in hidden and "L8425" in hidden and "L60" not in hidden
        assert r["hidden"] == 2
        # now top up a lot: balance covers everything -> restore the hidden ones
        g.fazer._balance = 1000.0
        calls.clear()
        r2 = g.check_once()
        shown = {lid for lid, a in calls if a is True}
        assert shown == {"L8100", "L8425"}
        assert r2["shown"] == 2
    finally:
        fo.set_lot_active = orig
    db.close()


def test_does_not_reshow_manually_hidden_lot(tmp_path):
    c, db, g, notes = _mk(tmp_path, 1000.0, mode="enforce")
    # mark L8100 as manually hidden (auto_hidden=0) in the guard table
    g._set_row("L8100", auto_hidden=0, cost=88.99, action="")
    from pubg_uc_spark.funpay import orders as fo
    calls = []
    orig = fo.set_lot_active
    fo.set_lot_active = lambda card, lid, active: (calls.append((lid, active)) or True)
    try:
        g.check_once()
        # balance is plenty, but L8100 was NOT auto-hidden -> never auto-shown
        assert all(lid != "L8100" for lid, a in calls)
    finally:
        fo.set_lot_active = orig
    db.close()


def test_balance_read_failure_touches_nothing(tmp_path):
    c, db, g, notes = _mk(tmp_path, None, mode="enforce")   # balance None
    from pubg_uc_spark.funpay import orders as fo
    calls = []
    orig = fo.set_lot_active
    fo.set_lot_active = lambda card, lid, active: (calls.append((lid, active)) or True)
    try:
        r = g.check_once()
        assert r["skipped"] == "баланс не прочитан"
        assert calls == []
    finally:
        fo.set_lot_active = orig
    db.close()


def test_hysteresis_margin_blocks_flap(tmp_path):
    # cost 88.99; margin 10% -> re-show needs balance >= 97.889.
    c, db, g, notes = _mk(tmp_path, 80.0, mode="enforce", margin_pct=10.0)
    from pubg_uc_spark.funpay import orders as fo
    calls = []
    orig = fo.set_lot_active
    fo.set_lot_active = lambda card, lid, active: (calls.append((lid, active)) or True)
    try:
        g.check_once()                                  # 80 < 88.99 -> hide 8100 (+8425)
        assert ("L8100", False) in calls
        calls.clear()
        g.fazer._balance = 92.0                          # affordable but < cost+margin
        g.check_once()
        assert all(lid != "L8100" for lid, a in calls)   # NOT re-shown yet (anti-flap)
        calls.clear()
        g.fazer._balance = 100.0                          # past the margin
        g.check_once()
        assert ("L8100", True) in calls                  # now restored
    finally:
        fo.set_lot_active = orig
    db.close()

