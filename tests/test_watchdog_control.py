"""Watchdog control tests: admin can enable/disable the watchdog and change the
silence threshold at runtime; the settings are persisted to the control file the
shell watchdog reads."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.plugin import Plugin  # noqa: E402
from pubg_uc_spark.services.watchdog_control import WatchdogControl  # noqa: E402
from pubg_uc_spark.utils.heartbeat import Heartbeat  # noqa: E402
from pubg_uc_spark.services.admin_service import AdminService  # noqa: E402


def _cfg(tmp_path):
    c = Config()
    c.watchdog_control_file = str(tmp_path / ".watchdog")
    c.watchdog_stall_minutes = 15
    return c


def test_defaults_when_no_file(tmp_path):
    wc = WatchdogControl(_cfg(tmp_path))
    d = wc.read()
    assert d["enabled"] is True
    assert d["stall_minutes"] == 15


def test_ensure_file_writes_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    wc = WatchdogControl(cfg)
    wc.ensure_file()
    assert os.path.isfile(cfg.watchdog_control_file)
    content = open(cfg.watchdog_control_file).read()
    assert "WATCHDOG_ENABLED=1" in content
    assert "STALL_MINUTES=15" in content


def test_toggle_and_stall_roundtrip(tmp_path):
    wc = WatchdogControl(_cfg(tmp_path))
    assert wc.set_enabled(False)
    assert wc.read()["enabled"] is False
    assert wc.set_stall_minutes(30)
    d = wc.read()
    assert d["enabled"] is False       # preserved across writes
    assert d["stall_minutes"] == 30
    assert wc.set_enabled(True)
    assert wc.read()["enabled"] is True
    assert wc.read()["stall_minutes"] == 30


def test_stall_minimum_is_one(tmp_path):
    wc = WatchdogControl(_cfg(tmp_path))
    wc.set_stall_minutes(0)
    assert wc.read()["stall_minutes"] == 1


def test_admin_watchdog_command(tmp_path):
    cfg = _cfg(tmp_path)
    wc = WatchdogControl(cfg)
    hb = Heartbeat(str(tmp_path / ".hb"))
    hb.beat(force=True)
    admin = AdminService(cfg, repo=None, order_service=None, watchdog=wc, heartbeat=hb)

    # status shows enabled + recent event
    status = admin.watchdog_cmd()
    assert "включён" in status
    assert "Последнее событие" in status

    # off / on
    assert "выключен" in admin.watchdog_cmd("off").lower()
    assert wc.read()["enabled"] is False
    assert "включ" in admin.watchdog_cmd("on").lower()
    assert wc.read()["enabled"] is True

    # stall
    assert "25" in admin.watchdog_cmd("stall", "25")
    assert wc.read()["stall_minutes"] == 25
    # bad argument -> usage hint (and stall value unchanged)
    assert "stall" in admin.watchdog_cmd("stall", "abc").lower()
    assert wc.read()["stall_minutes"] == 25


def test_admin_watchdog_unconfigured():
    admin = AdminService(Config(), repo=None, order_service=None)  # no watchdog
    assert "недоступ" in admin.watchdog_cmd().lower()


def test_plugin_creates_control_file_on_start(tmp_path):
    from conftest import FakeCardinal
    c = Config()
    c.spark_mock = True
    c.database_path = str(tmp_path / "wd.db")
    c.max_retries = 1
    c.code_pattern = r"[0-9]{9,11}"
    c.lots = {"37330959": LotConfig("37330959", "PUBG Mobile 60 UC", "60")}
    c.backfill_mode = "off"
    c.heartbeat_file = str(tmp_path / ".heartbeat")
    c.watchdog_control_file = str(tmp_path / ".watchdog")

    p = Plugin(FakeCardinal(), c, async_mode=False)
    p.start()
    assert os.path.isfile(c.watchdog_control_file)
    p.stop()
