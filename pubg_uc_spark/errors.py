"""Error taxonomy for the plugin (see task spec, section 11).

Three families, each with its own handling strategy:

* :class:`UserError`      - the buyer's fault or a definitive negative result
                            (bad code format, invalid code, already used,
                            account not found). NOT retried; a message is sent
                            to the buyer and the result is stored as final.
* :class:`TemporaryError` - transient failure (Spark timeout / 5xx / network,
                            temporary FunPay error). Retried by retry_service
                            up to MAX_RETRIES with backoff.
* :class:`CriticalError`  - our fault / unrecoverable (DB error, broken config,
                            unknown Spark response, auth error). NOT retried
                            silently; logged at CRITICAL level and surfaced to
                            the admin.
"""

from __future__ import annotations


class PluginError(Exception):
    """Base class for all plugin errors."""


class UserError(PluginError):
    """Definitive negative outcome tied to the buyer's input. Not retried."""


class TemporaryError(PluginError):
    """Transient error - eligible for retry."""


class CriticalError(PluginError):
    """Unrecoverable error - must reach the admin, never retried silently."""


# --- Spark-specific subclasses (still classified into the families above) ---

class SparkTemporaryError(TemporaryError):
    """Spark is unreachable / timed out / returned 5xx."""


class SparkCriticalError(CriticalError):
    """Spark returned something we cannot interpret, or auth failed."""
