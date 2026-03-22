from .paper_broker import PaperBroker

__all__ = ["PaperBroker", "PolymarketBroker"]


def __getattr__(name: str):
    if name == "PolymarketBroker":
        from .polymarket_broker import PolymarketBroker as _PolymarketBroker

        return _PolymarketBroker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
