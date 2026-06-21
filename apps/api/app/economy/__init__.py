"""Economy package (WS-1): coins, items, inventory, shop.

Owns the item catalog seed (``catalog``) and the FastAPI router lives in
``app.routers.economy``. The durable schema (``Item``, ``PlayerInventory``,
``ShopStock`` + ``Run.coins``) is defined in ``app.db.models`` and bootstrapped
by ``app.db.session.init_db``.
"""
