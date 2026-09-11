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


def test_stock_report_matches_real_data():
    c = Config()
    c.lots = _lots()
    c.stock_low_threshold = 10
    stock = {"60": 42, "325": 17, "660": 5, "1800": 0, "3850": 0, "8100": 0}
    admin = AdminService(c, repo=None, order_service=None)
    txt = admin.stock_report(stock)

    # max sellable per lot (floor of stock / packs, min over packs)
    assert "180 UC: 14 шт" in txt        # 42//3
    assert "1320 UC: 2 шт" in txt        # 5//2
    assert "1500 UC: 2 шт" in txt        # min(5//2, 42//3)=2
    assert "660 UC: 5 шт" in txt         # 660 pack = 5
    # 660 is the bottleneck (<=10) -> deficit; 60/325 are fine
    assert "660" in txt.split("Мало/нет:")[1]
    # unused packs (1800/3850/8100) are not warned about
    assert "1800" not in txt.split("Мало/нет:")[1]


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
