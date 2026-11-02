"""FazerCards provider: combo splitting, aggregation, and provider switch."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.fazer.checker import FazerChecker  # noqa: E402
from pubg_uc_spark.services.finance_store import FinanceStore  # noqa: E402
from pubg_uc_spark.spark.models import UnifiedStatus  # noqa: E402


class FakeFazerClient:
    """No-network stand-in exposing the surface FazerChecker uses."""

    def __init__(self, *, valid=True, fail_denoms=None, bad_player=False):
        self.valid = valid
        self.bad_player = bad_player
        self.fail_denoms = set(fail_denoms or [])
        self.orders = []            # (offer_id, idem)

    def validate_id(self, player_id, server_id=""):
        return {"valid": self.valid and not self.bad_player,
                "player_name": "Neo" if self.valid else ""}

    def resolve_offer_id(self, denom):
        return f"offer-{denom}"

    def create_order(self, offer_id, player_id, server_id, idem):
        self.orders.append((offer_id, idem))
        denom = offer_id.split("-")[1]
        if denom in self.fail_denoms:
            return {"order_id": f"O-{idem}", "order": {"status": "failed", "message": "oos"}}
        return {"order_id": f"O-{idem}", "order": {"status": "completed", "player_name": "Neo"}}

    def order_status(self, order_id):
        return {"status": "completed", "player_name": "Neo"}

    # classification helpers mirror the real client
    def is_ok(self, o):
        return str(o.get("status", "")).lower() == "completed"

    def is_failed(self, o):
        return str(o.get("status", "")).lower() in ("failed", "error")

    def is_bad_player(self, o):
        return False

    @staticmethod
    def fail_message(o):
        return str(o.get("fail_reason") or o.get("message") or "failed")

    @staticmethod
    def player_name_of(o):
        return o.get("player_name", "")


def _cfg():
    c = Config()
    c.fazer_validate_id = True
    return c


def test_single_pack_success():
    c = _cfg()
    chk = FazerChecker(c, client=FakeFazerClient())
    res = chk.redeem("100000001", {"60": 1}, idempotency_prefix="code7")
    assert res.status is UnifiedStatus.VALID
    assert res.delivered == 1 and res.total == 1
    assert res.player_name == "Neo"


def test_combo_lot_splits_into_multiple_orders():
    """A combo lot (8100+325) places one top-up order per base pack."""
    client = FakeFazerClient()
    chk = FazerChecker(_cfg(), client=client)
    res = chk.redeem("100000001", {"8100": 1, "325": 1}, idempotency_prefix="code9")
    assert res.status is UnifiedStatus.VALID
    assert res.delivered == 2 and res.total == 2
    offers = sorted(o[0] for o in client.orders)
    assert offers == ["offer-325", "offer-8100"]
    # deterministic idempotency keys per sub-order
    assert all(idem.startswith("code9:") for _o, idem in client.orders)


def test_quantity_multiplies_orders():
    client = FakeFazerClient()
    chk = FazerChecker(_cfg(), client=client)
    res = chk.redeem("100000001", {"60": 3}, idempotency_prefix="c")
    assert res.status is UnifiedStatus.VALID and res.delivered == 3
    assert len(client.orders) == 3


def test_partial_delivery():
    client = FakeFazerClient(fail_denoms={"325"})
    chk = FazerChecker(_cfg(), client=client)
    res = chk.redeem("100000001", {"8100": 1, "325": 1}, idempotency_prefix="c")
    assert res.status is UnifiedStatus.PARTIAL
    assert res.delivered == 1 and res.total == 2


def test_bad_uid_is_account_not_found_without_spending():
    client = FakeFazerClient(bad_player=True)
    chk = FazerChecker(_cfg(), client=client)
    res = chk.redeem("200000000", {"60": 1})
    assert res.status is UnifiedStatus.ACCOUNT_NOT_FOUND
    assert client.orders == []            # never placed an order


def test_validation_unsupported_proceeds_to_order():
    """When the category has no ID validation (HTTP 400), skip it and order."""
    class NoValidate(FakeFazerClient):
        def validate_id(self, player_id, server_id=""):
            return {"valid": True, "supported": False, "player_name": ""}
    client = NoValidate()
    chk = FazerChecker(_cfg(), client=client)
    res = chk.redeem("100000001", {"60": 1})
    assert res.status is UnifiedStatus.VALID
    assert len(client.orders) == 1        # ordered despite no validation


def test_mock_client_end_to_end():
    c = Config()                           # fazer_mock True (no key) by default
    assert c.fazer_mock is True
    chk = FazerChecker(c)                   # real client in mock mode
    res = chk.redeem("100000001", {"60": 2})
    assert res.status is UnifiedStatus.VALID and res.delivered == 2


def test_message_omits_nickname_when_absent():
    """FazerCards top-ups return no nickname -> drop the 'Имя игрока' line,
    but keep it when a name is present (e.g. Spark)."""
    from pubg_uc_spark.database.models import OrderRecord
    from pubg_uc_spark.services.order_service import OrderService
    c = Config()
    c.lots = {"L": LotConfig("L", "60 UC", "60", {"60": 1})}
    svc = OrderService(c, repo=None, messenger=None, retry_service=None)
    order = OrderRecord(funpay_order_id="O1", lot_id="L", quantity=1)
    tmpl = c.messages.valid
    assert "{player_name}" in tmpl
    with_name = svc._fmt(order, tmpl, uid="5101895432", player_name="Neo")
    no_name = svc._fmt(order, tmpl, uid="5101895432", player_name="")
    assert "Neo" in with_name and "Имя игрока" in with_name
    assert "Имя игрока" not in no_name     # blank line dropped


def test_provider_switch_persists(tmp_path):
    c = Config()
    c.finance_config_file = str(tmp_path / "finance.json")
    assert c.provider() == "spark"
    store = FinanceStore(c)
    store.load_into_cfg()
    store.set_delivery_provider("fazercards")
    assert c.provider() == "fazercards"
    # reload in a fresh config
    c2 = Config()
    c2.finance_config_file = str(tmp_path / "finance.json")
    c2.delivery_provider = "spark"
    FinanceStore(c2).load_into_cfg()
    assert c2.provider() == "fazercards"
