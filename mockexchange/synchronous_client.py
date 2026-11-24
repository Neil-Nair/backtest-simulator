# The trading client a bot talks to: exchange.connect(), insert_order(), get_last_price_book(), ...
# Every call is answered by the local simulation in _sim.py.
from . import _sim, _vclock
from .exchange_responses import AmendOrderResponse, DeleteOrderResponse, InsertOrderResponse

READ_SECONDS = 0.000002     # reads are treated as local cache reads: effectively instant
WRITE_SECONDS = 0.0012      # simulated round trip of insert / amend / delete


class Exchange:
    def __init__(self, *args, **kwargs):
        # Connection arguments are accepted and ignored: everything is local.
        _vclock.install()                 # make sure the process runs on virtual time
        self._sim = _sim.get_sim()
        self._connected = False

    def _call(self, seconds=READ_SECONDS):
        """Start of every API call: must be connected; then let simulated time pass."""
        assert self._connected and self._sim.connected, "Cannot call function until connected. Call connect() first"
        self._sim.api_call(seconds)
        return self._sim

    def _book(self, instrument_id):
        return self._call().books.get(instrument_id)

    # ---- connection
    def connect(self):
        self._sim.connected = True
        self._connected = True

    def disconnect(self):
        # disconnecting pulls all resting orders, positions stay
        for book in self._sim.books.values():
            book.own = {"bid": [], "ask": []}
            book.cached_book = None
        self._connected = False

    def is_connected(self):
        return self._connected and self._sim.connected

    # ---- orders
    def insert_order(self, instrument_id, *, price, volume, side, order_type="limit"):
        sim = self._call(WRITE_SECONDS)
        order_id, error = sim.insert(instrument_id, price, volume, side, order_type)
        return InsertOrderResponse(error is None, order_id, error)

    def amend_order(self, instrument_id, *, order_id, volume):
        error = self._call(WRITE_SECONDS).amend(instrument_id, order_id, volume)
        return AmendOrderResponse(error is None, error)

    def delete_order(self, instrument_id, *, order_id):
        error = self._call(WRITE_SECONDS).delete(instrument_id, order_id)
        return DeleteOrderResponse(error is None, error)

    def delete_orders(self, instrument_id):
        self._call(WRITE_SECONDS).delete(instrument_id)

    def get_outstanding_orders(self, instrument_id):
        book = self._book(instrument_id)
        return self._sim.outstanding(book) if book else {}

    # ---- market data
    def get_last_price_book(self, instrument_id):
        book = self._book(instrument_id)
        return self._sim.price_book(book) if book else None

    def poll_new_trade_ticks(self, instrument_id):
        book = self._book(instrument_id)
        self._sim.maybe_library_bug()          # raises BEFORE the queue is emptied: nothing is lost
        ticks = list(book.new_ticks) if book else []
        if book:
            book.new_ticks.clear()
        return ticks

    def get_trade_tick_history(self, instrument_id):
        book = self._book(instrument_id)
        return list(book.tick_history) if book else []

    def get_instruments(self):
        return dict(self._call().instruments)

    def get_tradable_instruments(self):
        return {i: inst for i, inst in self._call().instruments.items() if not inst.paused and not inst.expired}

    def poll_new_social_media_feeds(self):
        self._call()
        return []

    def get_social_media_feeds_history(self):
        self._call()
        return []

    # ---- our own trades, positions and money
    def poll_new_trades(self, instrument_id):
        book = self._book(instrument_id)
        self._sim.maybe_library_bug()
        trades = list(book.new_trades) if book else []          # LOSSY in bursts (stress option, see _sim.py)
        if book:
            book.new_trades.clear()
        return trades

    def get_trade_history(self, instrument_id):
        book = self._book(instrument_id)
        self._sim.maybe_library_bug()
        return list(book.trade_history) if book else []         # complete: our last 100 trades

    def get_positions(self):
        sim = self._call()
        return {i: sim.visible_position(book) for i, book in sim.books.items()}

    def get_positions_and_cash(self):
        sim = self._call()
        return {i: {"volume": sim.visible_position(book), "cash": book.cash} for i, book in sim.books.items()}

    def get_cash(self):
        return sum(book.cash for book in self._call().books.values())

    def get_pnl(self, valuations=None):
        return self._call().pnl(valuations)


# Threads: the virtual clock runs a bot's threads on time (see _vclock.py), so two threads can call the exchange at
# once. The simulation itself is not thread-safe, so each call runs alone.
def _serialised(method):
    import functools

    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with _vclock.API_LOCK:
            return method(self, *args, **kwargs)
    return call


for _name, _value in list(vars(Exchange).items()):
    if callable(_value) and not _name.startswith("_"):
        setattr(Exchange, _name, _serialised(_value))
