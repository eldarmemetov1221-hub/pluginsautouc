"""Finance: cost/commission calc, and the runtime-editable store (/uc_prices)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.database.db import Database  # noqa: E402
from pubg_uc_spark.database.models import OrderRecord, OrderStatus  # noqa: E402
from pubg_uc_spark.database.repository import Repository  # noqa: E402
from pubg_uc_spark.services.admin_service import AdminService  # noqa: E402
from pubg_uc_spark.services.finance_store import FinanceStore  # noqa: E402


def _cfg(tmp_path):
    c = Config()
    c.database_path = str(tmp_path / "fin.db")
    c.lots = {
        "60l": LotConfig("60l", "60 UC", "60", {"60": 1}),
        "720l": LotConfig("720l", "720 UC", "720", {"660": 1, "60": 1}),
    }
    c.commission_percent = 3.0
    c.pack_costs = {"60": 45.0, "660": 400.0}
    c.finance_config_file = str(tmp_path / "finance.json")
    return c


def test_order_cost_from_packs(tmp_path):
    c = _cfg(tmp_path)
    assert c.order_cost("60l", 1) == 45.0
    assert c.order_cost("60l", 3) == 135.0          # 60×3
    assert c.order_cost("720l", 1) == 445.0         # 660 + 60
    assert c.order_cost("unknown", 1) == 0.0


def test_finance_report_numbers(tmp_path):
    c = _cfg(tmp_path)
    db = Database(c.database_path)
    repo = Repository(db)
    # legacy rows (no cost snapshot) -> finance falls back to current pack costs
    repo.create_order(OrderRecord(funpay_order_id="A", lot_id="60l", quantity=1,
                                  status=OrderStatus.VALID.value, price=100))
    repo.create_order(OrderRecord(funpay_order_id="B", lot_id="720l", quantity=1,
                                  status=OrderStatus.VALID.value, price=900))
    admin = AdminService(c, repo, None)
    txt = admin.finance()
    # revenue 1000, commission 30 (3%), cost 45+445=490, net 480
    assert "1000.00" in txt
    assert "30.00" in txt
    assert "490.00" in txt
    assert "480.00" in txt
    db.close()


def test_finance_uses_frozen_cost_not_current(tmp_path):
    """Cost is frozen per order; changing pack costs must NOT recompute it."""
    c = _cfg(tmp_path)
    db = Database(c.database_path)
    repo = Repository(db)
    # order arrived when 60-pack cost was 45 -> frozen cost 45
    repo.create_order(OrderRecord(funpay_order_id="A", lot_id="60l", quantity=1,
                                  status=OrderStatus.VALID.value, price=100, cost=45))
    # now the seller raises the 60-pack cost to 90
    c.pack_costs = {"60": 90.0, "660": 400.0}
    admin = AdminService(c, repo, None)
    txt = admin.finance()
    # cost must stay 45 (frozen), NOT 90; net = 100 - 3 - 45 = 52
    assert "Себестоимость: −45.00 ₽" in txt
    assert "52.00" in txt
    db.close()


def test_finance_store_persists_and_reloads(tmp_path):
    c = _cfg(tmp_path)
    store = FinanceStore(c)
    store.load_into_cfg()                     # seeds finance.json
    assert os.path.isfile(c.finance_config_file)

    store.set_pack_cost("325", 210)
    store.set_commission(5)
    assert c.pack_costs["325"] == 210.0
    assert c.commission_percent == 5.0

    # a fresh config reloads the persisted values
    c2 = _cfg(tmp_path)
    c2.pack_costs = {}                         # wipe defaults to prove reload
    c2.commission_percent = 99.0
    FinanceStore(c2).load_into_cfg()
    assert c2.pack_costs.get("325") == 210.0
    assert c2.commission_percent == 5.0


def test_finance_store_seeds_when_missing(tmp_path):
    c = _cfg(tmp_path)
    assert not os.path.isfile(c.finance_config_file)
    FinanceStore(c).load_into_cfg()
    assert os.path.isfile(c.finance_config_file)   # created from current cfg


def test_auto_delivery_toggle_persists(tmp_path):
    c = _cfg(tmp_path)
    c.auto_delivery = True
    store = FinanceStore(c)
    store.load_into_cfg()
    store.set_auto_delivery(False)               # pause + persist
    assert c.auto_delivery is False

    # a fresh config (env default True) must pick the persisted pause back up
    c2 = _cfg(tmp_path)
    c2.auto_delivery = True
    FinanceStore(c2).load_into_cfg()
    assert c2.auto_delivery is False
