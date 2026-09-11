"""Stock report: parse Spark stock and compare to active lots."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.services.admin_service import AdminService  # noqa: E402


def _lots():
    raw = {
        "60l": ("60 UC", "60", {"60": 1}),
        "120l": ("120 UC", "120", {"60": 2}),
        "180l": ("180 UC", "180", {"60": 3}),
        "300l": ("300 UC", "300", {"325": 1}),
        "325l": ("325 UC", "325", {"325": 1}),
        "360l": ("360 UC", "360", {"325": 1, "60": 1}),
        "445l": ("445 UC", "445", {"325": 1, "60": 2}),
        "600l": ("600 UC", "600", {"660": 1}),
        "660l": ("660 UC", "660", {"660": 1}),
        "720l": ("720 UC", "720", {"660": 1, "60": 1}),
        "900l": ("900 UC", "900", {"660": 1, "60": 4}),
        "1045l": ("1045 UC", "1045", {"660": 1, "325": 1, "60": 1}),
        "1320l": ("1320 UC", "1320", {"660": 2}),
        "1500l": ("1500 UC", "1500", {"660": 2, "60": 3}),
    }
    return {k: LotConfig(k, p, uc, picks) for k, (p, uc, picks) in raw.items()}


# FunPay "Наличие" (listed availability) per lot, from the real screenshots.
_AMOUNTS = {"60l": 5, "120l": 6, "180l": 3, "300l": 3, "325l": 2, "360l": 5,
            "445l": 4, "600l": 3, "660l": 3, "720l": 3, "900l": 2, "1045l": 2,
            "1320l": 3, "1500l": 3}


def test_stock_report_availability_vs_stock():
    c = Config()
    c.lots = _lots()
    admin = AdminService(c, repo=None, order_service=None)
    stock = {"60": 42, "325": 17, "660": 5, "1800": 0, "3850": 0, "8100": 0}
    txt = admin.stock_report(stock, _AMOUNTS)

    # need: 60->61, 325->16, 660->25 ; stock 42/17/5 -> restock 19/0/20
    assert "нужно 25, в Spark 5 → не хватает 20" in txt     # 660
    assert "нужно 61, в Spark 42 → не хватает 19" in txt    # 60
    assert "нужно 16, в Spark 17" in txt                    # 325 ok
    assert "660×20" in txt and "60×19" in txt
    assert "325×" not in txt                                # no 325 restock
    assert "оштучно" not in txt                             # feature removed


def test_stock_report_total_cost_of_stock():
    c = Config()
    c.lots = _lots()
    c.pack_costs = {"60": 45.0, "325": 210.0, "660": 400.0}
    admin = AdminService(c, repo=None, order_service=None)
    stock = {"60": 42, "325": 17, "660": 5}
    txt = admin.stock_report(stock, _AMOUNTS)
    # 42*45 + 17*210 + 5*400 = 1890 + 3570 + 2000 = 7460
    assert "Себестоимость всего стока: 7460.00 ₽" in txt


def test_stock_report_unknown_amount_listed():
    c = Config()
    c.lots = _lots()
    admin = AdminService(c, repo=None, order_service=None)
    # 660l has no amount (None) -> reported as unreadable, excluded from need
    amounts = dict(_AMOUNTS)
    amounts["660l"] = None
    txt = admin.stock_report({"60": 42, "325": 17, "660": 5}, amounts)
    assert "Не удалось прочитать наличие" in txt
    assert "660 UC" in txt.split("Не удалось прочитать наличие")[1]


def test_stock_summary_parses_by_denomination(tmp_path):
    from pubg_uc_spark.spark.client import SparkChecker
    from pubg_uc_spark.spark import client as cli

    class FakeResp:
        status_code = 200
        def json(self):
            return {"by_denomination_uc": {"60": 42, "325": 17, "660": 5},
                    "denominations": [60, 325, 660]}

    fake = types.SimpleNamespace(get=lambda *a, **k: FakeResp())
    cli.requests = fake
    c = Config()
    c.spark_mock = False
    c.spark_api_url = "https://api.pubgredeemerbot.com"
    c.spark_api_key = "k"
    got = SparkChecker(c).stock_summary()
    assert got == {"60": 42, "325": 17, "660": 5}
