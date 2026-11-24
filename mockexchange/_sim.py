# The simulated market behind the mock client: hidden "true" prices, background liquidity,
# noise takers, price-time matching with our own orders in the same book, and the exchange rules.
# Prices inside the simulation are INTEGER TICKS (price / tick_size) so comparisons are exact.
#
# All instruments, prices and limits below are illustrative defaults for a synthetic market. Edit them to model
# whatever venue you want to test against.
import datetime
import json
import math
import os
import random
from collections import deque

from . import VIOLATIONS, _vclock
from . import _replay
from .common_types import (Instrument, InstrumentType, OptionKind, OrderStatus, PriceBook,
                           PriceChangeLimit, PriceVolume, Trade, TradeTick)

DT = 0.05                  # simulation step in seconds
R = 0.03                   # interest rate
POSITION_LIMIT = 100
OUTSTANDING_LIMIT = 200
RATE_LIMIT = 25            # order messages per rolling second
DELTA_LIMIT = 100.0        # soft delta limit per instrument group
DELTA_GRACE = 10.0         # seconds the delta may stay over the limit
BUSY_LOOP_CALLS = 20000    # API calls with no time.sleep in between = a busy loop
BURST_SECONDS = 0.1        # two of our fills on one instrument closer than this count as a burst

INDEX = "IDX5"             # a synthetic five-stock index with a future, an ETF and options on it
INDEX_WEIGHTS = {"STKB": 900.0, "STKA": 130.0, "STKC": 125.0, "STKE": 2250.0, "STKD": 950.0}
# stock: (start price, volatility per sqrt(second), extra half-spread in ticks, levels per side,
#         sweep size factor, trend speed per second (0 = never trends))
STOCKS = {
    "STKA": (143.70, 0.050, 3, 3, 0.3, 0.012), "STKB": (79.50, 0.013, 0, 6, 0.8, 0.0),
    "STKC": (60.00, 0.017, 0, 6, 1.0, 0.0), "STKD": (20.00, 0.008, 0, 7, 1.0, 0.0),
    "STKE": (24.90, 0.005, 0, 7, 1.0, 0.0), "STKF": (50.00, 0.016, 0, 6, 1.0, 0.0),
    "STKG": (40.00, 0.020, 0, 6, 1.0, 0.0), "STKH": (90.00, 0.025, 0, 4, 1.0, 0.0),
    "STKI": (80.30, 0.033, 0, 6, 0.7, 0.013), "STKJ": (30.00, 0.005, 0, 7, 1.0, 0.0),
}
EXPIRIES = {"202703": datetime.datetime(2027, 3, 19, 12), "202706": datetime.datetime(2027, 6, 18, 12),
            "202709": datetime.datetime(2027, 9, 17, 12)}
# kind: levels per side, level sizes, refresh interval (s), sweep interval (s), sweep size (lots),
#       how many ticks past the best a sweep goes, fair-value noise (mean, sd, relaxation time in s)
KINDS = {
    "stock":        (6, [50, 100, 150, 200, 200, 300, 400], (0.2, 1.0), (0.5, 1.2), (50, 600), 4, None),
    "dual":         (10, [20, 40, 40, 60, 80, 100], (0.2, 1.0), (0.5, 1.5), (10, 150), 4, (0.0, 0.02, 5.0)),
    "stock_future": (4, [100, 200, 300, 400], (0.3, 1.5), (1.0, 3.0), (20, 250), 1, (0.0, 0.02, 10.0)),
    "index_future": (5, [50, 100, 150, 200], (0.3, 1.5), (1.0, 4.0), (10, 120), 1, (0.0, 0.04, 10.0)),
    "etf":          (4, [200, 400, 600, 750], (0.3, 1.5), (0.5, 1.5), (50, 550), 1, (0.17, 0.05, 200.0)),
    "option":       (4, [100, 200, 300], (0.3, 1.5), (1.5, 3.5), (5, 50), 1, (0.0, 0.015, 10.0)),
}


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(S, K, T, sigma, is_call):
    """Returns (price, delta) of a European option."""
    d1 = (math.log(S / K) + (R + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm_cdf(d1) - K * math.exp(-R * T) * norm_cdf(d2), norm_cdf(d1)
    return K * math.exp(-R * T) * norm_cdf(-d2) - S * norm_cdf(-d1), norm_cdf(d1) - 1.0


def build_instruments():
    limit = PriceChangeLimit(5.0, 0.1)
    T = InstrumentType
    out = {}
    for s in STOCKS:
        out[s] = Instrument(s, 0.01, T.STOCK, limit, instrument_group=s)
    for s in ("STKB", "STKC"):
        out[s + "_DUAL"] = Instrument(s + "_DUAL", 0.01, T.STOCK, limit, instrument_group=s)
    for code, expiry in EXPIRIES.items():
        out["STKB_%s_F" % code] = Instrument("STKB_%s_F" % code, 0.1, T.STOCK_FUTURE, limit, expiry=expiry,
                                             base_instrument_id="STKB", interest_rate=R, instrument_group="STKB")
        out["%s_%s_F" % (INDEX, code)] = Instrument("%s_%s_F" % (INDEX, code), 0.1, T.INDEX_FUTURE, limit,
                                                    expiry=expiry, interest_rate=R, instrument_group=INDEX)
    out[INDEX + "_ETF"] = Instrument(INDEX + "_ETF", 0.1, T.INDEX_TRACKING_ETF, limit, index_id=INDEX,
                                     index_constituents=dict(INDEX_WEIGHTS), index_divisor=1000,
                                     etf_cash_comp=2.5, etf_multiplier=0.25, instrument_group=INDEX)
    expiry = EXPIRIES["202703"]
    for base, strikes, option_type in (("STKB", (50, 75, 100), T.STOCK_OPTION), (INDEX, (80, 100, 120), T.INDEX_OPTION)):
        for strike in strikes:
            for letter, kind in (("C", OptionKind.CALL), ("P", OptionKind.PUT)):
                name = "%s_202703_%03d%s" % (base, strike, letter)
                out[name] = Instrument(name, 0.1, option_type, limit, expiry=expiry, option_kind=kind,
                                       strike=float(strike), interest_rate=R, instrument_group=base,
                                       base_instrument_id="STKB" if base == "STKB" else None,
                                       index_volatility=1.5 if base == INDEX else None)
    return out


class OwnOrder:
    def __init__(self, order_id, side, tick, volume, ahead, seq):
        self.order_id, self.side, self.tick, self.volume = order_id, side, tick, volume
        self.ahead = ahead     # background lots queued in front of this order at its price
        self.seq = seq         # arrival number: earlier orders of ours fill first


class Book:
    """Everything the simulation keeps for one instrument."""

    def __init__(self, inst):
        self.inst, self.id, self.tick = inst, inst.instrument_id, inst.tick_size
        t = inst.instrument_type
        self.kind = {InstrumentType.STOCK: "stock", InstrumentType.STOCK_FUTURE: "stock_future",
                     InstrumentType.INDEX_FUTURE: "index_future", InstrumentType.INDEX_TRACKING_ETF: "etf"}.get(t, "option")
        self.stock = self.id                      # the stock whose hidden price drives this book
        if self.id.endswith("_DUAL"):
            self.kind, self.stock = "dual", self.id[:-5]
        self.params = KINDS[self.kind]
        self.levels, self.extra, self.flow = self.params[0], 0, 1.0
        if self.kind == "stock":
            _, _, self.extra, self.levels, self.flow, _ = STOCKS[self.id]
        if self.id == "STKC_DUAL" or self.id.startswith(INDEX + "_202706") or self.id.startswith(INDEX + "_202709"):
            self.flow = 0.5
        self.bg = {"bid": {}, "ask": {}}          # background liquidity: tick -> lots
        self.own = {"bid": [], "ask": []}         # our resting orders
        self.passive_lots = 0                     # lots of OUR resting orders that traded
        self.noise, self.noise_time = (self.params[6][0] if self.params[6] else 0.0), 0.0
        self.last_price, self.last_direction = 0.0, "bid"
        self.next_refresh, self.next_sweep = 0.0, 0.0
        self.cached_book = None                   # PriceBook, rebuilt only after a change
        self.position, self.cash = 0, 0.0
        self.new_trades, self.trade_history = deque(maxlen=5000), deque(maxlen=100)
        self.new_ticks, self.tick_history = deque(maxlen=5000), deque(maxlen=100)
        self.fill_count, self.fill_lots = 0, 0
        self.last_fill_time, self.poll_lost = -1e9, 0    # for the lossy poll_new_trades (see apply_fill)
        self.recent_fills = deque()               # (time, signed lots): fills the position cache has not shown yet


class Sim:
    def __init__(self, seed=None, quiet=False, start_positions=None):
        seed = int(os.environ.get("MOCK_SEED", "1")) if seed is None else seed
        if start_positions is None:
            start_positions = json.loads(os.environ.get("MOCK_START_POSITIONS", "") or "{}")
        self.rng = random.Random(seed)            # drives the market
        self.bug_rng = random.Random(seed + 7)    # drives the fake library bug, kept separate
        self.quiet = quiet                        # quiet = frozen market (used by the self-test)
        self.t0, self.t, self.steps = _vclock.elapsed, 0.0, 0
        self.connected = True
        # Positions and cash are exact and instant by default. Stress option: the position number shows a fill
        # of a RESTING order this many seconds late (fills of our own aggressive orders are always instant).
        # The RULES use the true position.
        self.position_lag = float(os.environ.get("MOCK_POSITION_LAG", "0") or 0)
        # Stress option: a fill that follows another fill on the same instrument within BURST_SECONDS is missing
        # from poll_new_trades with this probability. get_trade_history always has it.
        self.poll_drop = float(os.environ.get("MOCK_POLL_DROP", "0.08") or 0)
        self.drop_rng = random.Random(seed + 13)  # kept separate so the market does not change
        self.amend_ups = 0                        # amends that ENLARGED an order
        self.instruments = build_instruments()
        self.books = {i: Book(inst) for i, inst in self.instruments.items()}
        self.price = {s: STOCKS[s][0] for s in STOCKS}
        self.drift = {s: 0.0 for s in STOCKS}
        self.next_trend = {s: 0.0 for s in STOCKS}
        self.next_id, self.next_seq = 1, 1
        self.messages, self.message_total, self.message_max = deque(), 0, 0
        self.delta_since = {}                     # group -> time its |delta| went over the limit
        self.calls_since_sleep, self.seen_sleeps, self.busy = 0, _vclock.sleep_count, False
        self.replay = _replay.Replay(self) if _replay.FILES else None
        if self.replay is not None:
            self.replay_prices()
            for book in self.books.values():
                if not book.last_price:
                    book.last_price = self.replay.mid(book) or 0.0
                book.new_ticks.clear()                    # warm-up trades are history, not news for the bot
        for book in self.books.values():
            if self.replay is None:
                self.refresh(book)
                book.last_price = round(self.fair(book) / book.tick) * book.tick
                book.next_sweep = self.rng.uniform(0.0, book.params[3][1])
            book.position = int(start_positions.get(book.id, 0))
            book.cash = -book.position * book.last_price      # so PnL starts at zero

    # ---------------------------------------------------------------- time and fair values
    def now(self):
        return _vclock.elapsed - self.t0

    def timestamp(self):
        return _vclock.VirtualDateTime.now()

    def years_to(self, expiry):
        return (expiry - _vclock.START).total_seconds() / 86400.0 / 365.0 - (self.t0 + self.t) / 86400.0 / 365.0

    def index(self):
        return sum(w * self.price[s] for s, w in INDEX_WEIGHTS.items()) / 1000.0

    def underlying(self, book):
        if book.inst.instrument_group != INDEX:
            return self.price["STKB"]
        return self.index()

    def replay_prices(self):
        """Replay mode: the 'true' stock prices (used for deltas and the index) are the recorded mids."""
        for s in STOCKS:
            m = self.replay.mid(self.books[s])
            if m:
                self.price[s] = m

    def fair(self, book):
        """Hidden fair value of an instrument right now."""
        if self.replay is not None:
            return self.replay.mid(book) or book.last_price
        if book.kind == "stock":
            return self.price[book.id]
        mean, sd, tau = book.params[6]
        decay = math.exp(-(self.t - book.noise_time) / tau)     # slow mean-reverting noise
        book.noise = mean + (book.noise - mean) * decay + sd * math.sqrt(1.0 - decay * decay) * self.rng.gauss(0, 1)
        book.noise_time = self.t
        return self._value(book)

    def peek_fair(self, book):
        """The fair value WITHOUT drawing new noise (fair() draws random numbers, so calling it more
        often would change the market)."""
        if self.replay is not None:
            return self.replay.mid(book) or book.last_price
        if book.kind == "stock":
            return self.price[book.id]
        return self._value(book)

    def _value(self, book):
        if book.kind == "dual":
            return self.price[book.stock] + book.noise
        if book.kind == "etf":
            return 2.5 + 0.25 * self.index() + book.noise
        T = self.years_to(book.inst.expiry)
        if book.kind == "option":
            sigma = 1.5 if book.inst.instrument_group == INDEX else 3.0
            is_call = book.inst.option_kind == OptionKind.CALL
            return max(0.05, black_scholes(self.underlying(book), book.inst.strike, T, sigma, is_call)[0] + book.noise)
        return self.underlying(book) * math.exp(R * T) + book.noise

    def delta_per_lot(self, book):
        """Delta of one lot in units of its underlying."""
        if book.kind in ("stock", "dual"):
            return 1.0
        if book.kind == "etf":
            return 0.25
        T = self.years_to(book.inst.expiry)
        if book.kind == "option":
            sigma = 1.5 if book.inst.instrument_group == INDEX else 3.0
            return black_scholes(self.underlying(book), book.inst.strike, T, sigma,
                                 book.inst.option_kind == OptionKind.CALL)[1]
        return math.exp(R * T)

    def group_deltas(self):
        deltas = {}
        for book in self.books.values():
            if book.position:
                group = book.inst.instrument_group
                deltas[group] = deltas.get(group, 0.0) + book.position * self.delta_per_lot(book)
        return deltas

    # ---------------------------------------------------------------- market evolution
    def advance(self):
        """Catch the market up with the virtual clock."""
        target = self.now()
        if self.replay is not None:
            self.replay.feed_until(self.replay.now_ns())
            while self.t + DT <= target:
                self.t += DT
                self.steps += 1
                if self.steps % 10 == 0:
                    self.replay_prices()
                    self.check_delta()
            return
        while self.t + DT <= target:
            self.step()

    def step(self):
        self.t += DT
        self.steps += 1
        if not self.quiet:
            for s, (_, vol, _, _, _, trend) in STOCKS.items():
                if trend and self.t >= self.next_trend[s]:       # trending stocks switch trend now and then
                    self.drift[s] = trend * self.rng.choice((-1, 0, 1))
                    self.next_trend[s] = self.t + self.rng.uniform(20, 60)
                self.price[s] = max(1.0, self.price[s] + self.drift[s] * DT + vol * math.sqrt(DT) * self.rng.gauss(0, 1))
            for book in self.books.values():
                if self.t >= book.next_refresh:
                    self.refresh(book)
                if self.t >= book.next_sweep:
                    self.sweep(book)
        if self.steps % 10 == 0:
            self.check_delta()

    def refresh(self, book):
        """Background market makers re-quote around the current fair value. Until the next refresh
        the old quotes stay put, which is what creates short-lived stale prices."""
        rng = self.rng
        base = math.floor(self.fair(book) / book.tick)
        tops = {"bid": base, "ask": base + 1}                    # derivatives: 1 tick wide around fair
        if book.tick < 0.05:                                      # stocks: 1-5 ticks wide
            tops = {"bid": base - rng.randint(0, 2) - book.extra, "ask": base + 1 + rng.randint(0, 2) + book.extra}
        for side, step in (("bid", -1), ("ask", 1)):
            old, new = book.bg[side], {}
            for k in range(book.levels):
                size, gap = rng.choice(book.params[1]), rng.random() < 0.05
                tick = tops[side] + step * k
                if gap or tick <= 0:
                    continue
                # Volume still there keeps its place in the queue; a depleted level is refilled,
                # and the refill counts as arriving BEHIND our order (our 'ahead' is unchanged).
                new[tick] = old[tick] if old.get(tick, 0) >= 0.4 * size else size
            book.bg[side] = new
            for order in book.own[side]:
                order.ahead = min(order.ahead, new.get(order.tick, 0))
        # Our orders that the new quotes would cross get picked off at our price (adverse selection).
        for order in list(book.own["bid"]):
            if order.tick >= tops["ask"]:
                self.record_tick(book, order.tick, order.volume, "ask")
                self.fill_own(book, order, order.volume)
        for order in list(book.own["ask"]):
            if order.tick <= tops["bid"]:
                self.record_tick(book, order.tick, order.volume, "bid")
                self.fill_own(book, order, order.volume)
        book.next_refresh = self.t + rng.uniform(*book.params[2])
        book.cached_book = None

    def sweep(self, book):
        """A noise taker sweeps the book. Direction usually flips, so flow mean-reverts."""
        rng = self.rng
        chance_buy = 0.25 if book.last_direction == "bid" else 0.75
        drift = self.drift.get(book.stock, 0.0)
        if drift:
            chance_buy = 0.7 if drift > 0 else 0.3               # trending stock: one-way flow
        direction = "bid" if rng.random() < chance_buy else "ask"
        volume = max(1, int(rng.uniform(*book.params[4]) * book.flow))
        depth = rng.randint(0, book.params[5])
        book.next_sweep = self.t + rng.uniform(*book.params[3])
        book.last_direction = direction
        self.taker(book, direction, volume, depth)

    def taker(self, book, direction, volume, depth):
        """An outside aggressor buys ('bid') or sells ('ask') up to 'depth' ticks through the best
        price, but never further than a few ticks beyond fair value."""
        best = self.best(book, "ask" if direction == "bid" else "bid")
        if best is None:
            return
        fair_tick = self.fair(book) / book.tick
        reach = 8 + book.extra if book.tick < 0.05 else 2
        if direction == "bid":
            limit = min(best + depth, math.floor(fair_tick) + reach)
        else:
            limit = max(best - depth, math.ceil(fair_tick) - reach)
        self.take(book, direction, volume, limit)

    # ---------------------------------------------------------------- matching
    def best(self, book, side):
        if self.replay is not None:
            return self.replay.best(book, side)
        ticks = list(book.bg[side]) + [o.tick for o in book.own[side]]
        if not ticks:
            return None
        return max(ticks) if side == "bid" else min(ticks)

    def take(self, book, aggressor_side, volume, limit_tick):
        """Match an aggressive order against the resting side, best price first.
        Returns [(tick, lots)] executed by the aggressor."""
        if self.replay is not None:
            return self.replay.take(book, aggressor_side, volume, limit_tick)
        resting = "ask" if aggressor_side == "bid" else "bid"
        fills = []
        while volume > 0:
            best = self.best(book, resting)
            if best is None or (resting == "ask" and best > limit_tick) or (resting == "bid" and best < limit_tick):
                break
            traded = self.consume_level(book, resting, best, volume)
            volume -= traded
            fills.append((best, traded))
            self.record_tick(book, best, traded, aggressor_side)
        return fills

    def consume_level(self, book, side, tick, volume):
        """Trade up to 'volume' lots at one price level in time priority: background volume that was
        there before our order, then our order, then background volume that arrived later."""
        background = book.bg[side]
        ours = sorted((o for o in book.own[side] if o.tick == tick), key=lambda o: o.seq)
        traded = 0
        for order in ours:
            eaten = min(volume - traded, order.ahead, background.get(tick, 0))
            if eaten:
                background[tick] -= eaten
                traded += eaten
                for other in ours:
                    other.ahead = max(0, other.ahead - eaten)
            if traded == volume:
                break
            mine = min(volume - traded, order.volume)
            if mine < 1:
                continue
            traded += mine
            self.fill_own(book, order, mine)
        if traded < volume and background.get(tick, 0) > 0:
            eaten = min(volume - traded, background[tick])
            background[tick] -= eaten
            traded += eaten
        if background.get(tick, 1) <= 0:
            del background[tick]
        book.cached_book = None
        return traded

    def fill_resting(self, book, order, volume):
        """One of our resting orders trades (called by the replay engine too)."""
        return self.fill_own(book, order, volume)

    def fill_own(self, book, order, volume):
        """One of our resting orders trades. A big order is filled by several smaller
        counterparties: the lots arrive as a BURST of trades of 5-25 lots each."""
        book.passive_lots += volume
        order.volume -= volume
        if order.volume <= 0 and order in book.own[order.side]:
            book.own[order.side].remove(order)
        while volume > 0:
            piece = min(volume, self.drop_rng.randint(5, 25))
            self.apply_fill(book, order.side, order.tick * book.tick, piece, order.order_id)
            volume -= piece

    def apply_fill(self, book, side, price, volume, order_id, passive=True):
        """Book one of OUR fills: position, cash and the private trade record. passive = one of
        our resting orders was hit (False = our own order crossed the book). order_id None = the
        exchange closed the lots itself (soft delta limit): position and cash move, but there is
        no trade record."""
        sign = 1 if side == "bid" else -1
        book.position += sign * volume
        book.cash -= sign * volume * price
        book.cached_book = None
        if order_id is None:
            return
        book.fill_count += 1
        book.fill_lots += volume
        if self.position_lag > 0 and passive:
            book.recent_fills.append((self.now(), sign * volume))
        trade = Trade(self.timestamp(), order_id, self.new_id(), book.id, price, volume, side)
        in_burst, book.last_fill_time = self.t - book.last_fill_time < BURST_SECONDS, self.t
        if in_burst and self.drop_rng.random() < self.poll_drop:
            book.poll_lost += 1                      # poll_new_trades never reports this fill
        else:
            book.new_trades.append(trade)
        book.trade_history.append(trade)             # the history is complete (last 100)

    def record_tick(self, book, tick, volume, aggressor_side):
        book.last_price = tick * book.tick
        public = TradeTick(timestamp=self.timestamp(), instrument_id=book.id, price=book.last_price, volume=volume,
                           aggressor_side=aggressor_side, buyer="", seller="", trade_id=self.new_id())
        book.new_ticks.append(public)
        book.tick_history.append(public)

    def new_id(self):
        self.next_id += 1
        return self.next_id

    # ---------------------------------------------------------------- rules
    def breach(self, text, disconnect):
        VIOLATIONS.append((round(self.now(), 3), text + (" -> DISCONNECTED" if disconnect else "")))
        if disconnect:
            for book in self.books.values():
                book.own = {"bid": [], "ask": []}
                book.cached_book = None
            self.connected = False

    def api_call(self, seconds):
        """Called at the start of every client call: charge its duration, spot busy loops, catch up."""
        if _vclock.sleep_count != self.seen_sleeps:
            self.seen_sleeps, self.calls_since_sleep = _vclock.sleep_count, 0
        self.calls_since_sleep += 1
        if self.calls_since_sleep > BUSY_LOOP_CALLS:
            if not self.busy:
                self.busy = True
                self.breach("busy loop: %d API calls without a time.sleep" % BUSY_LOOP_CALLS, False)
            seconds = max(seconds, 0.01)      # force time forward so the run still ends
        _vclock.advance(seconds)
        self.advance()

    def count_message(self):
        """Every insert/amend/delete is one message. More than RATE_LIMIT in a rolling second disconnects."""
        now = self.now()
        self.messages.append(now)
        while self.messages[0] <= now - 1.0:
            self.messages.popleft()
        self.message_total += 1
        self.message_max = max(self.message_max, len(self.messages))
        if len(self.messages) > RATE_LIMIT:
            self.breach("rate limit: %d order messages within one second" % len(self.messages), True)
        return self.connected

    def insert(self, instrument_id, price, volume, side, order_type):
        """Returns (order_id or None, error text or None)."""
        book = self.books.get(instrument_id)
        if not self.count_message():
            return None, "disconnected: rate limit"
        if book is None or side not in ("bid", "ask") or order_type not in ("limit", "ioc"):
            self.breach("bad order arguments: %r %r %r" % (instrument_id, side, order_type), False)
            return None, "bad instrument, side or order type"
        what = "%s %s %s x %s" % (instrument_id, side, price, volume)
        if not isinstance(volume, int) or volume < 1 or volume > 100000:
            self.breach("volume outside [1, 100000]: " + what, False)
            return None, "invalid volume"
        ticks = price / book.tick
        if abs(ticks - round(ticks)) > 1e-6:
            self.breach("price not on the tick grid: " + what, False)
            return None, "price not on tick"
        change = book.inst.price_change_limit
        allowed = min(change.absolute_change, change.relative_change * book.last_price)
        if abs(price - book.last_price) > allowed + 1e-9 or price < 0 or price > 1000000:
            self.breach("price outside price_change_limit (last %.2f, allowed +-%.2f): %s" % (book.last_price, allowed, what), False)
            return None, "price outside price change limit"
        same_side = sum(o.volume for o in book.own[side])
        both_sides = same_side + sum(o.volume for o in book.own["ask" if side == "bid" else "bid"])
        room = POSITION_LIMIT - book.position if side == "bid" else POSITION_LIMIT + book.position
        if both_sides + volume > OUTSTANDING_LIMIT:
            self.breach("more than %d lots outstanding: %s with %d resting" % (OUTSTANDING_LIMIT, what, both_sides), True)
            return None, "disconnected: outstanding order limit"
        if same_side + volume > room:
            self.breach("worst-case position limit: %s with position %d and %d lots resting on that side"
                        % (what, book.position, same_side), True)
            return None, "disconnected: position limit"
        order_id, tick = self.new_id(), round(ticks)
        filled = 0
        fills = self.take(book, side, volume, tick)                          # crosses the book first
        for fill_tick, lots in fills:
            self.apply_fill(book, side, fill_tick * book.tick, lots, order_id, passive=False)
            filled += lots
        if filled < volume and order_type == "limit":
            # The rest joins the queue behind whatever is already resting at that price.
            self.next_seq += 1
            book.own[side].append(OwnOrder(order_id, side, tick, volume - filled, book.bg[side].get(tick, 0), self.next_seq))
            book.cached_book = None
        return order_id, None

    def find_order(self, instrument_id, order_id):
        book = self.books.get(instrument_id)
        if book is not None:
            for order in book.own["bid"] + book.own["ask"]:
                if order.order_id == order_id:
                    return book, order
        return book, None

    def amend(self, instrument_id, order_id, volume):
        if not self.count_message():
            return "disconnected: rate limit"
        book, order = self.find_order(instrument_id, order_id)
        if order is None:
            return "order not found (already filled or deleted?)"
        if not isinstance(volume, int) or volume < 1:
            self.breach("amend to a bad volume: %s order %s to %r" % (instrument_id, order_id, volume), False)
            return "invalid volume"
        if volume > order.volume:
            # An amend may enlarge an order (it keeps its queue place). The worst case is checked with the new
            # volume, and a breach disconnects, as an insert would.
            self.amend_ups += 1
            extra, side = volume - order.volume, order.side
            same_side = sum(o.volume for o in book.own[side])
            both_sides = same_side + sum(o.volume for o in book.own["ask" if side == "bid" else "bid"])
            room = POSITION_LIMIT - book.position if side == "bid" else POSITION_LIMIT + book.position
            if both_sides + extra > OUTSTANDING_LIMIT or same_side + extra > room:
                self.breach("amend enlarged order %s on %s from %d to %d past a limit (position %d, %d lots resting "
                            "on that side)" % (order_id, instrument_id, order.volume, volume, book.position, same_side), True)
                return "disconnected: limit breached by an amend"
        order.volume = volume
        book.cached_book = None
        return None

    def delete(self, instrument_id, order_id=None):
        """Delete one order, or all orders on the instrument if order_id is None. One message."""
        if not self.count_message():
            return "disconnected: rate limit"
        book, order = self.find_order(instrument_id, order_id)
        if book is None or (order_id is not None and order is None):
            return "order not found (already filled or deleted?)"
        if order_id is None:
            book.own = {"bid": [], "ask": []}
        else:
            book.own[order.side].remove(order)
        book.cached_book = None
        return None

    def check_delta(self):
        """Soft delta limit: |net delta| of a group >= DELTA_LIMIT for more than DELTA_GRACE s costs 10% of it,
        closed at 2% worse than mid."""
        deltas = self.group_deltas()
        for group in list(self.delta_since):
            if abs(deltas.get(group, 0.0)) < DELTA_LIMIT:
                del self.delta_since[group]
        for group, delta in deltas.items():
            if abs(delta) < DELTA_LIMIT or self.t - self.delta_since.setdefault(group, self.t) <= DELTA_GRACE:
                continue
            self.delta_since[group] = self.t
            # close the lots in the instrument that contributes most to the excess
            members = [b for b in self.books.values() if b.inst.instrument_group == group and b.position]
            book = max(members, key=lambda b: b.position * self.delta_per_lot(b) * (1 if delta > 0 else -1))
            per_lot = abs(self.delta_per_lot(book)) or 1.0
            lots = min(abs(book.position), max(1, int(round(0.1 * abs(delta) / per_lot))))
            side = "ask" if book.position > 0 else "bid"
            bid, ask = self.best(book, "bid"), self.best(book, "ask")
            mid = (bid + ask) / 2.0 * book.tick if bid is not None and ask is not None else book.last_price
            price = mid * (0.98 if side == "ask" else 1.02)
            self.apply_fill(book, side, price, lots, None)
            self.breach("soft delta limit: group %s delta %.1f for over %.0f s; exchange closed %d %s at %.2f (mid %.2f)"
                        % (group, delta, DELTA_GRACE, lots, book.id, price, mid), False)

    # ---------------------------------------------------------------- views for the client / report
    def price_book(self, book):
        if book.cached_book is None:
            sides = {}
            for side in ("bid", "ask"):
                lots = self.replay.depth(book, side) if self.replay is not None else dict(book.bg[side])
                for order in ([] if self.replay is not None else book.own[side]):
                    lots[order.tick] = lots.get(order.tick, 0) + order.volume
                sides[side] = [PriceVolume(t * book.tick, lots[t]) for t in sorted(lots, reverse=(side == "bid"))]
            book.cached_book = PriceBook(timestamp=self.timestamp(), instrument_id=book.id,
                                         bids=sides["bid"], asks=sides["ask"])
        return book.cached_book

    def visible_position(self, book):
        """The position as the client's cache shows it: fills younger than position_lag are missing."""
        while book.recent_fills and book.recent_fills[0][0] <= self.now() - self.position_lag:
            book.recent_fills.popleft()
        return book.position - sum(lots for _, lots in book.recent_fills)

    def kick(self):
        """Simulate another process connecting with the same account: we are cut off and our orders are
        pulled. Not a rule breach, so nothing is added to VIOLATIONS."""
        for book in self.books.values():
            book.own = {"bid": [], "ask": []}
            book.cached_book = None
        self.connected = False
        self.messages_at_kick = self.message_total      # lets a test prove nothing was sent afterwards

    def outstanding(self, book):
        return {o.order_id: OrderStatus(o.order_id, book.id, o.tick * book.tick, o.volume, o.side)
                for o in book.own["bid"] + book.own["ask"]}

    def pnl(self, valuations=None, use_fair=False):
        total = 0.0
        for book in self.books.values():
            value = self.fair(book) if use_fair else (valuations or {}).get(book.id, book.last_price)
            total += book.cash + book.position * value
        return total

    def maybe_library_bug(self):
        """Stress option: polling now and then raises, as a client library with a background thread can."""
        if not self.quiet and self.bug_rng.random() < 1.0 / 300.0:
            raise RuntimeError("deque mutated during iteration")


CURRENT = None      # the one simulated exchange of this process


def get_sim():
    global CURRENT
    if CURRENT is None:
        CURRENT = Sim()
    return CURRENT


def reset(seed=None, quiet=False, start_positions=None):
    """Start a fresh market (used by the self-test)."""
    global CURRENT
    del VIOLATIONS[:]
    CURRENT = Sim(seed, quiet, start_positions)
    return CURRENT
