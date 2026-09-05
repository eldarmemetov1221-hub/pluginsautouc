"""Test fixtures. The core plugin is exercised WITHOUT FunPayCardinal via a
fake ``cardinal`` object that records sent messages and serves fake orders.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.plugin import Plugin  # noqa: E402


class FakeOrderShortcut:
    """Mirrors FunPayAPI.types.OrderShortcut (matched by description)."""

    def __init__(self, order_id, description, buyer_id, buyer_username, amount=1, chat_id="chat-1"):
        self.id = order_id
        self.description = description
        self.buyer_id = str(buyer_id)
        self.buyer_username = buyer_username
        self.amount = amount
        self.chat_id = chat_id


class FakeAccount:
    def __init__(self):
        self.id = "seller-1"


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
    c.lots = {"37330959": LotConfig("37330959", "PUBG Mobile 60 UC", "60")}
    c.admin_ids = [111]
    # Keep all runtime state files inside the test's tmp dir (never touch the
    # package directory, which is the production default).
    c.backfill_mode = "off"
    c.backfill_trigger_file = str(tmp_path / ".backfill_request")
    c.heartbeat_file = str(tmp_path / ".heartbeat")
    c.watchdog_control_file = str(tmp_path / ".watchdog")
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
    """Return a NEW_ORDER shortcut. lot_id="37330959" -> a matching 60 UC
    description; anything else -> a non-matching description."""
    if str(lot_id) == "37330959":
        description = "PUBG Mobile 60 UC (id игрока)"
    else:
        description = "Some other product 999"
    return FakeOrderShortcut(order_id, description, buyer_id, username, amount, chat_id)


class FakeMessage:
    def __init__(self, message_id, author_id, chat_id, text, by_bot=False):
        self.id = message_id
        self.author_id = author_id
        self.chat_id = chat_id
        self.text = text
        self.by_bot = by_bot
