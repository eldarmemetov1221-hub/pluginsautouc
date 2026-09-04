"""Test fixtures. The core plugin is exercised WITHOUT FunPayCardinal via a
fake ``cardinal`` object that records sent messages and serves fake orders.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.plugin import Plugin  # noqa: E402


class FakeFullOrder:
    def __init__(self, order_id, lot_id, buyer_id, buyer_username, amount=1, chat_id="chat-1"):
        self.id = order_id
        self.lot_id = str(lot_id)
        self.buyer_id = str(buyer_id)
        self.buyer_username = buyer_username
        self.amount = amount
        self.chat_id = chat_id


class FakeOrderShortcut:
    def __init__(self, order_id):
        self.id = order_id


class FakeAccount:
    def __init__(self):
        self.id = "seller-1"
        self._orders = {}

    def register(self, full_order):
        self._orders[str(full_order.id)] = full_order

    def get_order(self, order_id):
        return self._orders.get(str(order_id))


class FakeCardinal:
    def __init__(self):
        self.account = FakeAccount()
        self.telegram = None
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))

    # convenience for assertions
    def texts_to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.spark_mock = True
    c.database_path = str(tmp_path / "test.db")
    c.max_retries = 3
    c.retry_delay = 0.0
    c.retry_backoff = 1.0
    c.code_pattern = r"[0-9]{9,11}"
    c.lots = {"37330959": LotConfig("37330959", "PUBG Mobile 60 UC", 60)}
    c.admin_ids = [111]
    return c


@pytest.fixture
def cardinal():
    return FakeCardinal()


@pytest.fixture
def plugin(cfg, cardinal):
    p = Plugin(cardinal, cfg, async_mode=False)  # inline processing for tests
    yield p
    p.stop()


def make_order(cardinal, order_id, lot_id="37330959", buyer_id="buyer-1",
               username="john", amount=1, chat_id="chat-1"):
    """Register a fake full order and return its NEW_ORDER shortcut."""
    cardinal.account.register(
        FakeFullOrder(order_id, lot_id, buyer_id, username, amount, chat_id)
    )
    return FakeOrderShortcut(order_id)


class FakeMessage:
    def __init__(self, message_id, author_id, chat_id, text, by_bot=False):
        self.id = message_id
        self.author_id = author_id
        self.chat_id = chat_id
        self.text = text
        self.by_bot = by_bot
