# Replay exchange: the simulated market is replaced by a RECORDED market (order-event records).
#
# Switched on by MOCK_REPLAY=<orders file(s)>. Every recorded order arrives at its recorded time, size and price; the
# bot's orders join the same queues. Only the market changes: the client API, the rules (position, outstanding volume,
# message rate, soft delta limit), the clock and the report are the simulator's.
#
# How a fill of OUR resting order is decided (queue-aware "shadow" replay):
#   - The recorded book is kept exactly as recorded. When a recorded order arrives that crosses the best opposite price
#     (recorded orders and ours together), it is an AGGRESSOR. It walks the opposite side best price first; at each
#     price, the resting orders trade in arrival order. Recorded orders take their share of it (as they really did),
#     and our order gets what is left when the aggressor reaches it. So we are filled only if the real incoming volume
#     was big enough to get through everything that was queued in front of us.
#   - Our own aggressive orders (IOC, or a limit that crosses) trade against the recorded resting volume visible at
#     that moment. Volume we take is remembered per recorded order, so it cannot be taken twice.
# The big limitation: the recorded traders do not react to us. If our order takes part of an aggressor, the recorded
# orders behind us still get the fills the record says they got, and nobody re-prices because of our quotes. For
# small or moderate sizes the effect is small; for large size changes the replay flatters us. Calibrate against a
# live run (see tools/calibrate.py) before trusting a number.
#
# Settings (environment):
#   MOCK_REPLAY=a.csv.gz[,b.csv.gz]  order records: timestamp_ns,instrument_id,order_id,price,volume,side,action,order_type
#                                    (action: new, fill, partialFill, amended, cancelled; .csv or .csv.gz; time-sorted)
#   MOCK_REPLAY_WARMUP=120           seconds of the record replayed BEFORE the bot starts, to fill the books (orders placed
#                                    before the window are unknown; most resting orders are young)
#   MOCK_REPLAY_EXCLUDE=fills.csv    fills files of a bot that traded LIVE in the recorded window (column order_id): those
#                                    orders are removed, so the replayed bot does not compete with its own past self.
#                                    Live orders that never filled cannot be identified and stay in (a small bias).
#   MOCK_REPLAY_OWN_GRACE=1.0        seconds for which volume that the LIVE bot's aggressive orders took from other
#                                    resting orders stays available in the replay ("ghost" volume, see below)
#
# Knock-on fills of the excluded live orders: removing them does not remove the fills they CAUSED. When a live IOC hit a
# resting quote, the record shrinks that quote ~1 microsecond later. Applied as recorded, the replayed bot would lose
# that volume to its own past self. So a fill/partial fill within 1 ms of one of the excluded aggressive orders, on the
# side it hit, is treated as OURS: the lots stay visible for MOCK_REPLAY_OWN_GRACE seconds, and what the replayed bot
# takes in that time counts against the same lots. After the grace the record's view applies again.
import csv
import gzip
import heapq
import io
import os
from collections import OrderedDict

from . import _vclock

FILES = [f for f in os.environ.get("MOCK_REPLAY", "").split(",") if f]
WARMUP = float(os.environ.get("MOCK_REPLAY_WARMUP", "120") or 0)
EXCLUDE = [f for f in os.environ.get("MOCK_REPLAY_EXCLUDE", "").split(",") if f]
GRACE = float(os.environ.get("MOCK_REPLAY_OWN_GRACE", "1.0") or 0)
OWN_WINDOW_NS = 1_000_000                                 # knock-on fills of our live order arrive within ~1 microsecond
OTHER = {"bid": "ask", "ask": "bid"}


def _open(path):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), newline="")
    return open(path, newline="")


def read_events(paths):
    """Time-ordered events from one or more time-sorted files: (ns, instrument, order_id, price, volume, side, action, type)."""
    def one(path):
        with _open(path) as f:
            for row in csv.DictReader(f):
                try:
                    yield (int(row["timestamp_ns"]), row["instrument_id"], row["order_id"], float(row["price"] or 0),
                           int(float(row["volume"] or 0)), row["side"], row["action"], row.get("order_type", "limit"))
                except (KeyError, ValueError):
                    continue
    return heapq.merge(*(one(p) for p in paths), key=lambda e: e[0])


def read_excluded(paths):
    ids = set()
    for path in paths:
        with _open(path) as f:
            for row in csv.DictReader(f):
                if row.get("order_id"):
                    ids.add(str(row["order_id"]).split(".")[0])
    return ids


def first_timestamp(paths):
    for event in read_events(paths):
        return event[0]
    raise ValueError("MOCK_REPLAY: no events in %s" % ",".join(paths))


class Replay:
    """The recorded market, attached to a Sim. Holds the recorded orders per book and feeds them in as the clock runs."""

    def __init__(self, sim):
        self.sim = sim
        sim.replay = self                                 # sim.best() and friends must see the replay during warm-up
        self.excluded = read_excluded(EXCLUDE)
        start_ns = first_timestamp(FILES) + int(WARMUP * 1e9)
        _vclock.set_start_ns(start_ns)                    # the bot's clock starts at the end of the warm-up
        self.start_ns = start_ns
        self.events = read_events(FILES)
        self.pending = None
        self.applied = self.skipped_own = self.unknown = self.aggressors = self.ghosted = 0
        self.own_aggr = {}                                # instrument -> (ns, side) of our last excluded aggressive order
        self.expiries = []                                # heap of (ns, instrument, order_id) when ghost volume ends
        self.last_ns = None
        for book in sim.books.values():
            book.h = {"bid": {}, "ask": {}}               # tick -> OrderedDict(order_id -> lots), arrival order
            book.horders = {}                             # order_id -> (side, tick, seq)
            book.taken = {}                               # order_id -> lots our aggressive orders took from it
            book.ghost = {}                               # order_id -> lots our LIVE past self took, still visible
            book.last_price = 0.0
        self.feed_until(start_ns)                         # warm-up: fill the books before the bot starts

    # ------------------------------------------------------------------ feeding the record
    def now_ns(self):
        return self.start_ns + int(_vclock.elapsed * 1e9)

    def feed_until(self, ns):
        while True:
            while self.expiries and self.expiries[0][0] <= ns and (self.pending is None or self.expiries[0][0] <= self.pending[0]):
                self.expire(*heapq.heappop(self.expiries))
            if self.pending is None:
                self.pending = next(self.events, None)
                if self.pending is None:
                    return
            if self.pending[0] > ns:
                return
            self.apply(self.pending)
            self.pending = None

    def apply(self, event):
        ns, name, oid, price, volume, side, action, otype = event
        self.last_ns = ns
        book = self.sim.books.get(name)
        if book is None or side not in ("bid", "ask"):
            return
        if oid in self.excluded:
            self.skipped_own += 1
            if action == "new":
                opposite = self.sim.best(book, OTHER[side])
                tick = int(round(price / book.tick))
                if otype != "limit" or (opposite is not None and (tick >= opposite if side == "bid" else tick <= opposite)):
                    self.own_aggr[name] = (ns, side)
            return
        self.applied += 1
        tick = int(round(price / book.tick))
        if action == "new":
            left = volume
            opposite = self.sim.best(book, OTHER[side])
            if opposite is not None and (tick >= opposite if side == "bid" else tick <= opposite):
                self.aggressors += 1
                left = volume - self.aggressor(book, side, tick, volume)
            if otype == "limit" and left > 0:
                self.sim.next_seq += 1
                book.horders[oid] = (side, tick, self.sim.next_seq)
                book.h[side].setdefault(tick, OrderedDict())[oid] = left
        elif action in ("fill", "partialFill", "amended", "cancelled"):
            known = book.horders.get(oid)
            if known is None:
                self.unknown += 1                         # placed before the record starts
                return
            s, t, _ = known
            remaining = 0 if action in ("fill", "cancelled") else volume
            level = book.h[s].get(t)
            own = self.own_aggr.get(name)
            if (GRACE > 0 and action in ("fill", "partialFill") and own is not None and ns - own[0] <= OWN_WINDOW_NS
                    and s == OTHER[own[1]] and level is not None and oid in level):
                cut = level[oid] - remaining                  # lots our live past self took from this order
                if cut > 0:
                    self.ghosted += cut
                    book.ghost[oid] = book.ghost.get(oid, 0) + cut
                    level[oid] = remaining                    # may be 0: the order lives on as ghost volume only
                    heapq.heappush(self.expiries, (ns + int(GRACE * 1e9), name, oid))
                    book.cached_book = None
                    return
            book.ghost.pop(oid, None)                         # any other event: the record's view of this order applies
            if remaining > 0 and level is not None:
                level[oid] = remaining                    # an enlarged order keeps its place
            else:
                if level is not None:
                    level.pop(oid, None)
                    if not level:
                        del book.h[s][t]
                del book.horders[oid]
                book.taken.pop(oid, None)
        book.cached_book = None

    def expire(self, ns, name, oid):
        """Ghost volume ends: our past self's take and the replayed bot's take were the same lots, so only the part of
        the bot's take beyond the ghost still counts."""
        book = self.sim.books[name]
        g = book.ghost.pop(oid, 0)
        if not g:
            return
        if oid in book.taken:
            book.taken[oid] = max(0, book.taken[oid] - g)
        known = book.horders.get(oid)
        if known is not None:
            level = book.h[known[0]].get(known[1])
            if level is not None and level.get(oid, 1) <= 0:  # filled in the record: gone now
                level.pop(oid, None)
                if not level:
                    del book.h[known[0]][known[1]]
                del book.horders[oid]
                book.taken.pop(oid, None)
        book.cached_book = None

    # ------------------------------------------------------------------ matching
    def visible(self, book, oid, lots):
        return max(0, lots + book.ghost.get(oid, 0) - book.taken.get(oid, 0))

    def queue(self, book, side, tick):
        """Everything resting at one price in arrival order: ('h', oid, lots, seq) for recorded, ('us', order) for ours."""
        items = []
        for oid, lots in book.h[side].get(tick, {}).items():
            v = self.visible(book, oid, lots)
            if v:
                items.append((book.horders[oid][2], "h", oid, v))
        for order in book.own[side]:
            if order.tick == tick:
                items.append((order.seq, "us", order, order.volume))
        items.sort(key=lambda x: x[0])
        return items

    def levels(self, book, side):
        ticks = set(book.h[side]) | {o.tick for o in book.own[side]}
        return sorted(ticks, reverse=(side == "bid"))

    def aggressor(self, book, side, limit, volume):
        """A recorded order crosses: it trades best price first, in arrival order. Returns the lots it executed.
        Recorded resting orders are NOT reduced here (the record's own fill events do that); ours are filled."""
        resting, left = OTHER[side], volume
        for tick in self.levels(book, resting):
            if left <= 0 or (tick > limit if side == "bid" else tick < limit):
                break
            traded = 0
            for item in self.queue(book, resting, tick):
                if left <= 0:
                    break
                if item[1] == "h":
                    eat = min(left, item[3])
                else:
                    order = item[2]
                    eat = min(left, order.volume)
                    self.sim.aggressor = "market"
                    self.sim.fill_resting(book, order, eat)
                left -= eat
                traded += eat
            if traded:
                self.sim.record_tick(book, tick, traded, side)
        return volume - left

    def take(self, book, side, volume, limit):
        """OUR aggressive order: trades against the recorded resting volume (and our own resting orders, as the mock
        allows self-trades). Returns [(tick, lots)]."""
        resting, left, fills = OTHER[side], volume, []
        for tick in self.levels(book, resting):
            if left <= 0 or (tick > limit if side == "bid" else tick < limit):
                break
            traded = 0
            for item in self.queue(book, resting, tick):
                if left <= 0:
                    break
                eat = min(left, item[3])
                if item[1] == "h":
                    book.taken[item[2]] = book.taken.get(item[2], 0) + eat
                else:
                    self.sim.fill_resting(book, item[2], eat)
                left -= eat
                traded += eat
            if traded:
                fills.append((tick, traded))
                self.sim.record_tick(book, tick, traded, side)
        book.cached_book = None
        return fills

    def best(self, book, side):
        ticks = [t for t, lv in book.h[side].items() if any(self.visible(book, o, v) for o, v in lv.items())]
        ticks += [o.tick for o in book.own[side]]
        if not ticks:
            return None
        return max(ticks) if side == "bid" else min(ticks)

    def depth(self, book, side):
        lots = {}
        for t, lv in book.h[side].items():
            v = sum(self.visible(book, o, x) for o, x in lv.items())
            if v:
                lots[t] = v
        for order in book.own[side]:
            lots[order.tick] = lots.get(order.tick, 0) + order.volume
        return lots

    def mid(self, book):
        b, a = self.best(book, "bid"), self.best(book, "ask")
        if b is None or a is None:
            return book.last_price or None
        return (a + b) / 2.0 * book.tick

    def summary(self):
        import datetime
        began = datetime.datetime.fromtimestamp(self.start_ns / 1e9 - WARMUP, datetime.timezone.utc)
        return ("replay: record from %s, bot started after %.0f s of warm-up; %d recorded events applied (%d aggressive "
                "orders); %d events of our own live orders removed (%d lots they took kept as ghost volume for %.1f s); %d events for orders placed before the record ignored%s"
                % (began.strftime("%d %b %H:%M:%S UTC"), WARMUP, self.applied, self.aggressors, self.skipped_own,
                   self.ghosted, GRACE, self.unknown, "" if self.pending is not None else
                   "; THE RECORD ENDED before the run did (the market froze after that)"))
