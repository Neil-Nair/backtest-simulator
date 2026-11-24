# Data types returned by the client: instruments, order books, trades, order status.
import enum


class InstrumentType(enum.Enum):
    STOCK = 1
    STOCK_OPTION = 2
    STOCK_FUTURE = 3
    INDEX_TRACKING_ETF = 4
    INDEX_OPTION = 5
    INDEX_FUTURE = 6


class OptionKind(enum.Enum):
    PUT = 1
    CALL = 2


class PriceChangeLimit:
    def __init__(self, absolute_change, relative_change):
        self.absolute_change = absolute_change
        self.relative_change = relative_change


class PriceLimit:
    def __init__(self, min, max):
        self.min = min
        self.max = max


class Instrument:
    def __init__(self, instrument_id, tick_size, instrument_type=None, price_change_limit=None, *,
                 expiry=None, option_kind=None, strike=None, base_instrument_id=None,
                 interest_rate=None, index_id=None, index_constituents=None, index_divisor=None,
                 index_volatility=None, etf_cash_comp=None, etf_multiplier=None,
                 instrument_group=None, price_limit=None):
        self.instrument_id = instrument_id
        self.tick_size = tick_size
        self.instrument_type = instrument_type
        self.price_change_limit = price_change_limit
        self.expiry = expiry
        self.option_kind = option_kind
        self.strike = strike
        self.base_instrument_id = base_instrument_id
        self.interest_rate = interest_rate
        self.index_id = index_id
        self.index_constituents = index_constituents
        self.index_divisor = index_divisor
        self.index_volatility = index_volatility
        self.etf_cash_comp = etf_cash_comp
        self.etf_multiplier = etf_multiplier
        self.instrument_group = instrument_group
        self.price_limit = price_limit
        self.paused = False
        self.expired = False

    def __repr__(self):
        return "Instrument(%s, tick=%s, %s)" % (self.instrument_id, self.tick_size, self.instrument_type)


class PriceVolume:
    def __init__(self, price, volume):
        self.price = price
        self.volume = volume

    def __repr__(self):
        return "PriceVolume(price=%s, volume=%s)" % (self.price, self.volume)


class PriceBook:
    def __init__(self, *, timestamp=None, instrument_id=None, bids=None, asks=None):
        self.timestamp = timestamp
        self.instrument_id = instrument_id
        self.bids = bids if bids is not None else []   # best (highest) first
        self.asks = asks if asks is not None else []   # best (lowest) first

    def __repr__(self):
        return "PriceBook(%s, bids=%s, asks=%s)" % (self.instrument_id, self.bids, self.asks)


class OrderStatus:
    def __init__(self, order_id=None, instrument_id=None, price=None, volume=None, side=None):
        self.order_id = order_id
        self.instrument_id = instrument_id
        self.price = price
        self.volume = volume
        self.side = side

    def __repr__(self):
        return "OrderStatus(order_id=%s, %s, price=%s, volume=%s, side=%s)" % (
            self.order_id, self.instrument_id, self.price, self.volume, self.side)


class Trade:
    """A private trade (one of OUR fills). side 'bid' = we bought, 'ask' = we sold."""

    def __init__(self, timestamp=None, order_id=None, trade_id=None, instrument_id=None,
                 price=None, volume=None, side=None):
        self.timestamp = timestamp
        self.order_id = order_id
        self.trade_id = trade_id
        self.instrument_id = instrument_id
        self.price = price
        self.volume = volume
        self.side = side

    def __repr__(self):
        return "Trade(%s, price=%s, volume=%s, side=%s, order_id=%s)" % (
            self.instrument_id, self.price, self.volume, self.side, self.order_id)


class TradeTick:
    """A public trade. aggressor_side 'bid' = the aggressor bought."""

    def __init__(self, *, timestamp=None, instrument_id=None, price=None, volume=None,
                 aggressor_side=None, buyer=None, seller=None, trade_id=None):
        self.timestamp = timestamp
        self.instrument_id = instrument_id
        self.price = price
        self.volume = volume
        self.aggressor_side = aggressor_side
        self.buyer = buyer
        self.seller = seller
        self.trade_id = trade_id

    def __repr__(self):
        return "TradeTick(%s, price=%s, volume=%s, aggressor_side=%s)" % (
            self.instrument_id, self.price, self.volume, self.aggressor_side)


class SocialMediaFeed:
    def __init__(self, *, timestamp=None, post=None, meta_data=None):
        self.timestamp = timestamp
        self.post = post
        self.meta_data = meta_data
