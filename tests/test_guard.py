"""BalanceGuard variant C: dynamic stock (наличие) capped to the wallet."""

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


class _LF:
    def __init__(self, lot_id, amount, active):
        self.lot_id = lot_id
        self.amount = amount
        self.active = active
        self.fields = {}


class FakeCardinal:
    """Real funpay helpers drive this via get_lot_fields / save_lot."""
    def __init__(self, state):
        self.state = {str(k): dict(v) for k, v in state.items()}
        self.saves = []

        class _Acc:
            def __init__(self, outer):
                self.o = outer

            def get_lot_fields(self, key):
                st = self.o.state[str(key)]
                return _LF(key, st["amount"], st["active"])

            def save_lot(self, lf):
                self.o.state[str(lf.lot_id)] = {"amount": int(lf.amount), "active": bool(lf.active)}
                self.o.saves.append((str(lf.lot_id), int(lf.amount), bool(lf.active)))
        self.account = _Acc(self)


def _mk(tmp_path, balance, state, *, mode="enforce"):
    c = Config()
    c.database_path = str(tmp_path / "g.db")
    c.delivery_provider = "fazercards"
    c.balance_guard_enabled = True
    c.balance_guard_mode = mode
    c.lots = {
        "L60": LotConfig("L60", "60 UC", "60", {"60": 1}),
        "L8100": LotConfig("L8100", "8100 UC", "8100", {"8100": 1}),
    }
    db = Database(c.database_path)
    notes = []
    card = FakeCardinal(state)
    g = BalanceGuard(c, db, FakeFazer(balance), card,
                     notifier=lambda t: notes.append(t), async_mode=False)
    return c, db, g, card, notes


def test_cost_calc_including_combo(tmp_path):
    c, db, g, card, notes = _mk(tmp_path, 1000,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}})
    c.lots["L8425"] = LotConfig("L8425", "8425 UC", "8425", {"8100": 1, "325": 1})
    card.state["L8425"] = {"amount": 1, "active": True}
    costs = {e["lot_id"]: e["cost"] for e in g.check_once()["lots"]}
    assert costs["L60"] == 0.88 and costs["L8100"] == 88.99
    assert costs["L8425"] == round(88.99 + 4.44, 4)
    db.close()


def test_enforce_caps_stock_to_affordable(tmp_path):
    # balance 3 -> covers floor(3/0.88)=3 of 60 UC; declared 8 -> cap to 3.
    # 8100 ($88.99) -> 0 affordable -> hidden.
    c, db, g, card, notes = _mk(tmp_path, 3.0,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}})
    g.check_once()
    assert card.state["L60"] == {"amount": 3, "active": True}     # capped 8 -> 3
    assert card.state["L8100"]["active"] is False                 # hidden
    db.close()


def test_restore_full_stock_after_topup(tmp_path):
    c, db, g, card, notes = _mk(tmp_path, 3.0,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}})
    g.check_once()                                                # caps 60->3, hides 8100
    assert card.state["L60"]["amount"] == 3
    g.fazer._balance = 1000.0
    g.check_once()                                                # plenty -> restore
    assert card.state["L60"] == {"amount": 8, "active": True}     # back to desired 8
    assert card.state["L8100"] == {"amount": 2, "active": True}   # 8100 restored + active
    db.close()


def test_no_write_when_already_correct(tmp_path):
    c, db, g, card, notes = _mk(tmp_path, 1000.0,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}})
    g.check_once()
    assert card.saves == []        # everything affordable & already at desired -> no edits
    db.close()


def test_follows_seller_amount_change_when_ample(tmp_path):
    c, db, g, card, notes = _mk(tmp_path, 1000.0,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}})
    g.check_once()                                   # captures desired=8, managed=8
    card.state["L60"]["amount"] = 15                 # seller raises наличие by hand
    g.check_once()
    assert g._row("L60")["desired_amount"] == 15     # desired follows the seller
    db.close()


def test_monitor_never_edits(tmp_path):
    c, db, g, card, notes = _mk(tmp_path, 3.0,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}}, mode="monitor")
    g.check_once()
    assert card.saves == []                          # monitor never touches FunPay
    assert any("хватает на" in n or "не хватает" in n for n in notes)
    db.close()


def test_balance_read_failure_touches_nothing(tmp_path):
    c, db, g, card, notes = _mk(tmp_path, None,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}})
    r = g.check_once()
    assert r["skipped"] == "баланс не прочитан"
    assert card.saves == []
    db.close()


def test_provider_not_fazer_skips(tmp_path):
    c, db, g, card, notes = _mk(tmp_path, 3.0,
                                {"L60": {"amount": 8, "active": True},
                                 "L8100": {"amount": 2, "active": True}})
    c.delivery_provider = "spark"
    r = g.check_once()
    assert r["skipped"] and card.saves == []
    db.close()
