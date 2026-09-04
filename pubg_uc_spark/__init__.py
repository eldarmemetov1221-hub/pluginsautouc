"""PUBG Mobile UC / Spark auto-checker plugin for FunPayCardinal.

This is the CORE package. It is deliberately importable and testable WITHOUT
FunPayCardinal / FunPayAPI being installed: every FunPay-specific import is
performed lazily inside the FunPay adapter layer (``pubg_uc_spark.funpay``)
and inside ``pubg_uc_spark.plugin``.

Layout (see project README):

    config.py          - all tunable parameters (env / .env)
    plugin.py          - FunPayCardinal glue (events -> services)
    funpay/            - FunPay adapters (orders, messenger)
    spark/             - Spark HTTP adapter: client + parser + models
    database/          - SQLite: db, models (+FSM), repository
    services/          - order_service, code_service, retry_service
    utils/             - logger (with masking), validators (CODE_PATTERN)
"""

__version__ = "0.1.0"
