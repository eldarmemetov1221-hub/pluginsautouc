"""Heartbeat tests: the plugin proves the FunPay runner is alive by bumping a
timestamp file at startup and on every event, so a watchdog can detect a
stalled runner (process alive but no events)."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.plugin import Plugin  # noqa: E402
from pubg_uc_spark.utils.heartbeat import Heartbeat  # noqa: E402

from conftest import FakeMessage, make_order  # noqa: E402


def test_heartbeat_beat_and_age(tmp_path):
    hb = Heartbeat(str(tmp_path / ".hb"))
    assert hb.age() is None            # not written yet
    hb.beat(force=True)
    age = hb.age()
    assert age is not None and age < 2


def test_heartbeat_disabled_when_no_path(tmp_path):
    hb = Heartbeat("")
    hb.beat(force=True)                # must not raise
    assert hb.age() is None


def test_heartbeat_coalesces_writes(tmp_path):
    path = tmp_path / ".hb"
    hb = Heartbeat(str(path), min_interval=60.0)
    hb.beat(force=True)
    first = path.read_text()
    time.sleep(0.01)
    hb.beat()                          # within min_interval -> no rewrite
    assert path.read_text() == first
    hb.beat(force=True)                # force -> rewrite allowed
    # (value may be identical second, but the call path is exercised)


def _plugin(tmp_path):
    c = Config()
    c.spark_mock = True
    c.database_path = str(tmp_path / "hb.db")
    c.max_retries = 1
    c.retry_delay = 0.0
    c.code_pattern = r"[0-9]{9,11}"
    c.lots = {"37330959": LotConfig("37330959", "PUBG Mobile 60 UC", "60")}
    c.backfill_mode = "off"
    c.heartbeat_file = str(tmp_path / ".heartbeat")
    c.watchdog_control_file = str(tmp_path / ".watchdog")

    from conftest import FakeCardinal
    p = Plugin(FakeCardinal(), c, async_mode=False)
    p.start()
    return p


def test_startup_and_events_bump_heartbeat(tmp_path):
    p = _plugin(tmp_path)
    # startup beat happened
    assert p.heartbeat.age() is not None

    # disable write-coalescing so each event write is observable in the test
    # (in production events are seconds apart, so coalescing never masks a beat)
    p.heartbeat._min_interval = 0.0

    # backdate the file, then an event should refresh it
    old = int(time.time()) - 9999
    with open(p.cfg.heartbeat_file, "w") as fh:
        fh.write(str(old))
    assert p.heartbeat.age() > 9000

    p.on_new_order(make_order(p.cardinal, "ORDHB", lot_id="other"))  # untracked, still a beat
    assert p.heartbeat.age() < 5

    with open(p.cfg.heartbeat_file, "w") as fh:
        fh.write(str(old))
    p.on_new_message(FakeMessage("m1", "buyer-x", "chat-x", "просто чат"))
    assert p.heartbeat.age() < 5
    p.stop()
