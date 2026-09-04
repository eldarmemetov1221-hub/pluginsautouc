"""FunPayCardinal entry point for the PUBG UC / Spark auto-checker plugin.

Drop this file into FunPayCardinal's ``plugins/`` directory together with the
``pubg_uc_spark/`` package (place the package on the Python path - the simplest
is next to this file inside the FPC root, which is already importable).

This module is intentionally thin: it declares the FPC plugin metadata and the
event bindings, and delegates all logic to the ``pubg_uc_spark`` package.
"""

from __future__ import annotations

import os
import sys

# Make sure the core package is importable regardless of how FPC loads plugins.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, os.path.dirname(_HERE)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from pubg_uc_spark import __version__  # noqa: E402
from pubg_uc_spark import plugin as _impl  # noqa: E402

# --- FunPayCardinal plugin metadata --------------------------------------- #
NAME = "PUBG UC Spark Auto-Checker"
VERSION = __version__
DESCRIPTION = (
    "Автоматическая обработка заказов PUBG Mobile UC: приём кода от покупателя, "
    "проверка через Spark, ответ покупателю, учёт кодов и защита от повторной "
    "обработки."
)
CREDITS = "@pubg_uc_spark"
UUID = "8f3a2c10-9b7e-4d5a-8c21-1f6e37330959"
SETTINGS_PAGE = False


def _init(cardinal, *args):
    _impl.init(cardinal)


def _new_order(cardinal, event, *args):
    _impl.on_new_order(cardinal, event)


def _new_message(cardinal, event, *args):
    _impl.on_new_message(cardinal, event)


# --- Event bindings -------------------------------------------------------- #
BIND_TO_PRE_INIT = [_init]
BIND_TO_NEW_ORDER = [_new_order]
BIND_TO_NEW_MESSAGE = [_new_message]
BIND_TO_DELETE = None
