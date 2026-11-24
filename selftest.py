# Self-test of the simulated exchange. Prints one PASS/FAIL line per check.
#   python selftest.py
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockexchange
from mockexchange import _sim, _vclock
from mockexchange.synchronous_client import Exchange

failures = 0


def check(name, condition, detail=""):
    global failures
    failures += 0 if condition else 1
    print("%s  %s %s" % ("PASS" if condition else "FAIL", name, detail))


def fresh(quiet=True, **options):
    """New market. quiet=True freezes it so a test controls every trade itself."""
    sim = _sim.reset(seed=1, quiet=quiet, **options)
    exchange = Exchange()
    exchange.connect()
    return sim, exchange


def best(exchange, stock="STKD"):
    book = exchange.get_last_price_book(stock)
    return book.bids[0], book.asks[0]


def thin_out_best_ask(sim, exchange, stock="STKD"):
    """An outsider buys all but 10 lots of the best ask, so a small order of ours can clear it."""
    ask = best(exchange, stock)[1]
    sim.take(sim.books[stock], "bid", ask.volume - 10, round(ask.price / 0.01))


# --- virtual clock
_vclock.install()
real_before, virtual_before = _vclock.REAL_TIME(), time.time()
time.sleep(5)
check("clock: sleep(5) advances time.time by 5 s", abs(time.time() - virtual_before - 5) < 1e-6)
check("clock: sleep(5) takes no real time", _vclock.REAL_TIME() - real_before < 0.5)
now = datetime.datetime.now()
check("clock: datetime.now() follows the virtual clock", now.year == 2026 and now.hour == 18 and now.minute == 30, str(now))
years = (datetime.datetime(2027, 3, 19, 12) - datetime.datetime.now()) / datetime.timedelta(days=1) / 365
check("clock: time to March expiry is 0.3527 years", abs(years - 0.3527) < 0.0002, "%.4f" % years)

# --- instruments and book
sim, ex = fresh()
instruments = ex.get_instruments()
check("31 instruments", len(instruments) == 31, str(len(instruments)))
check("option attributes", instruments["IDX5_202703_120P"].strike == 120 and instruments["IDX5_202703_120P"].index_volatility == 1.5
      and instruments["STKB_202703_050C"].base_instrument_id == "STKB")
bid, ask = best(ex)
check("book has both sides and is not crossed", bid.price < ask.price, "%s / %s" % (bid, ask))

# --- queue position: joining a level puts us BEHIND the volume already there
queue = bid.volume
order = ex.insert_order("STKD", price=bid.price, volume=5, side="bid", order_type="limit").order_id
check("book includes our own order", best(ex)[0].volume == queue + 5)
check("order is outstanding", ex.get_outstanding_orders("STKD")[order].volume == 5)
sim.take(sim.books["STKD"], "ask", queue - 1, 0)          # an outsider sells just less than the queue
check("queue: no fill while volume ahead remains", ex.get_positions()["STKD"] == 0)
sim.take(sim.books["STKD"], "ask", 3, 0)                  # 1 lot left ahead of us, then 2 for us
check("queue: filled only after the queue ahead is gone", ex.get_positions()["STKD"] == 2, str(ex.get_positions()["STKD"]))
trades = ex.poll_new_trades("STKD")
check("poll_new_trades reports the fill once", len(trades) == 1 and trades[0].volume == 2 and trades[0].side == "bid"
      and trades[0].order_id == order and ex.poll_new_trades("STKD") == [])
check("trade history keeps it", len(ex.get_trade_history("STKD")) == 1)

# --- amend keeps the place in the queue; an amend to a LARGER volume is accepted and counted
check("amend up is accepted and counted", ex.amend_order("STKD", order_id=order, volume=50).success
      and ex.get_outstanding_orders("STKD")[order].volume == 50 and sim.amend_ups == 1)
check("amend down works", ex.amend_order("STKD", order_id=order, volume=1).success
      and ex.get_outstanding_orders("STKD")[order].volume == 1)
sim.take(sim.books["STKD"], "ask", 1, 0)
check("amended order kept its queue position", ex.get_positions()["STKD"] == 3 and not ex.get_outstanding_orders("STKD"))
del mockexchange.VIOLATIONS[:]

# --- a better price is first in line
sim, ex = fresh()
bid, ask = best(ex)
ex.insert_order("STKD", price=bid.price + 0.01, volume=5, side="bid", order_type="limit")
sim.take(sim.books["STKD"], "ask", 2, 0)
check("priority: an order at a new better price trades first", ex.get_positions()["STKD"] == 2)
ticks = ex.poll_new_trade_ticks("STKD")
check("public tick shows aggressor side and empty names", len(ticks) == 1 and ticks[0].aggressor_side == "ask" and ticks[0].buyer == "")
ex.delete_orders("STKD")
check("delete_orders clears our orders", ex.get_outstanding_orders("STKD") == {})

# --- crossing limit order and IOC
sim, ex = fresh()
thin_out_best_ask(sim, ex)
bid, ask = best(ex)
ex.insert_order("STKD", price=ask.price, volume=ask.volume + 7, side="bid", order_type="limit")
rest = ex.get_outstanding_orders("STKD")
check("crossing limit order trades at once and rests the remainder",
      ex.get_positions()["STKD"] == ask.volume and sum(o.volume for o in rest.values()) == 7)
cash = ex.get_positions_and_cash()["STKD"]["cash"]
check("cash and pnl are booked", abs(cash + ask.volume * ask.price) < 1e-6 and abs(ex.get_pnl()) < 1e-6, "cash %.2f" % cash)
sim, ex = fresh()
thin_out_best_ask(sim, ex)
bid, ask = best(ex)
response = ex.insert_order("STKD", price=ask.price, volume=ask.volume + 7, side="bid", order_type="ioc")
check("IOC fills what it can and cancels the rest",
      response.success and ex.get_positions()["STKD"] == ask.volume and ex.get_outstanding_orders("STKD") == {})
check("no violations so far", mockexchange.VIOLATIONS == [], str(mockexchange.VIOLATIONS))

# --- soft rejections (order refused, still connected)
sim, ex = fresh()
bid, ask = best(ex)
bad = [ex.insert_order("STKD", price=bid.price + 0.005, volume=1, side="bid"),
       ex.insert_order("STKD", price=bid.price, volume=0, side="bid"),
       ex.insert_order("STKD", price=bid.price - 6.0, volume=1, side="bid")]
check("off-tick, zero volume and far-away price are refused and recorded",
      not any(r.success for r in bad) and len(mockexchange.VIOLATIONS) == 3 and ex.is_connected())

# --- hard breaches (disconnect)
sim, ex = fresh()
bid, ask = best(ex)
ex.insert_order("STKD", price=bid.price, volume=60, side="bid")
check("60 resting bids is fine", mockexchange.VIOLATIONS == [] and ex.is_connected())
response = ex.insert_order("STKD", price=bid.price - 0.01, volume=50, side="bid")
check("worst-case position breach is recorded and disconnects",
      not response.success and len(mockexchange.VIOLATIONS) == 1 and not ex.is_connected(), str(mockexchange.VIOLATIONS))
try:
    ex.get_positions()
    check("calls after a disconnect raise AssertionError", False)
except AssertionError as error:
    check("calls after a disconnect raise AssertionError", "Cannot call function until connected" in str(error))

sim, ex = fresh(start_positions={"STKD": 40})
bid, ask = best(ex)
ex.insert_order("STKD", price=ask.price + 0.05, volume=140, side="ask")      # allowed: 100 + 40
check("position counts: 140 asks allowed when long 40", ex.is_connected())
ex.insert_order("STKD", price=bid.price - 0.05, volume=60, side="bid")       # allowed: 100 - 40, total now 200
check("200 lots outstanding is allowed", ex.is_connected())
ex.insert_order("STKD", price=bid.price - 0.05, volume=1, side="bid")
check("201 lots outstanding disconnects", not ex.is_connected(), str(mockexchange.VIOLATIONS))

# --- message rate
sim, ex = fresh()
bid, ask = best(ex)
for burst in range(3):
    for i in range(20):
        ex.insert_order("STKD", price=bid.price - 0.03, volume=1, side="bid")
    ex.delete_orders("STKD")
    time.sleep(1.0)
check("21 messages per second, three times: fine", ex.is_connected() and sim.message_max == 21, str(sim.message_max))
for i in range(13):
    ex.insert_order("STKD", price=bid.price - 0.03, volume=1, side="bid")
    ex.insert_order("STKD", price=ask.price + 0.03, volume=1, side="ask")
check("26 messages in one second disconnects", not ex.is_connected() and "rate limit" in mockexchange.VIOLATIONS[-1][1])

# --- soft delta limit
sim, ex = fresh(start_positions={"IDX5_ETF": -100, "IDX5_202703_F": 25})
check("delta of -100 ETF +25 future is 0.27", abs(sim.group_deltas()["IDX5"] - 0.27) < 0.02, "%.3f" % sim.group_deltas()["IDX5"])
sim, ex = fresh(start_positions={"STKE": 100})
time.sleep(9)
ex.get_positions()
check("delta of 100 for 9 s: no penalty yet", mockexchange.VIOLATIONS == [])
time.sleep(3)
check("delta of 100 for over 10 s: 10% closed at a 2% worse price",
      ex.get_positions()["STKE"] == 90 and len(mockexchange.VIOLATIONS) == 1 and ex.get_pnl() < -4, "pnl %.2f" % ex.get_pnl())

# --- what the exchange closes itself is a booking, not a trade
check("the forced close moved position and cash but left no trade record", ex.get_trade_history("STKE") == []
      and ex.poll_new_trades("STKE") == [] and ex.get_positions_and_cash()["STKE"]["volume"] == 90)

# --- an amend that enlarges an order past the worst-case limit disconnects, like an insert would
sim, ex = fresh()
bid, ask = best(ex)
order = ex.insert_order("STKD", price=bid.price - 0.02, volume=60, side="bid").order_id
ex.insert_order("STKD", price=bid.price - 0.03, volume=30, side="bid")
check("amend 60 -> 70 with 30 more resting: allowed (100 in the worst case)",
      ex.amend_order("STKD", order_id=order, volume=70).success and ex.is_connected())
response = ex.amend_order("STKD", order_id=order, volume=71)
check("amend 70 -> 71: worst case 101, recorded and disconnected",
      not response.success and not ex.is_connected() and "amend enlarged" in mockexchange.VIOLATIONS[-1][1], str(mockexchange.VIOLATIONS))

# --- poll_new_trades loses fills in bursts; the trade history, positions and cash do not
sim, ex = fresh()
sim.poll_drop = 0.5                                        # exaggerated, so that the test is sure to see it
polled = history_ids = 0
for round_number in range(20):
    bid, ask = best(ex)
    order = ex.insert_order("STKD", price=bid.price + 0.01, volume=90, side="bid").order_id
    sim.take(sim.books["STKD"], "ask", 90, 0)              # one seller, but the 90 lots trade in pieces
    polled += sum(t.volume for t in ex.poll_new_trades("STKD"))
    history = ex.get_trade_history("STKD")
    check_ids = [t.trade_id for t in history if t.order_id == order]
    history_ids += sum(t.volume for t in history if t.order_id == order)
    ex.insert_order("STKD", price=ask.price - 0.01, volume=90, side="ask")
    sim.take(sim.books["STKD"], "bid", 90, 10 ** 9)        # and a buyer takes us back to flat
    ex.poll_new_trades("STKD")
    time.sleep(0.2)                                        # stay inside the message rate
check("a 90-lot fill arrives as a burst of several trades", len(check_ids) >= 3, "%d trades" % len(check_ids))
check("poll_new_trades lost part of the bursts", polled < 20 * 90, "reported %d of %d lots" % (polled, 20 * 90))
check("get_trade_history had every lot, positions are exact", history_ids == 20 * 90 and ex.get_positions()["STKD"] == 0,
      "%d lots" % history_ids)
check("the history keeps the last 100 trades", len(ex.get_trade_history("STKD")) == 100)
cash = ex.get_positions_and_cash()["STKD"]["cash"]
check("cash per instrument is exact", abs(cash - sim.books["STKD"].cash) < 1e-9 and abs(ex.get_pnl() - cash) < 1e-9)
sim, ex = fresh()
sim.poll_drop = 0.5
bid, ask = best(ex)
for i in range(30):                                        # single small fills, far apart: never lost
    ex.insert_order("STKD", price=ask.price, volume=1, side="bid", order_type="ioc")
    time.sleep(0.5)
    bid, ask = best(ex)
check("single fills far apart are all reported by poll_new_trades", len(ex.poll_new_trades("STKD")) == 30)
del mockexchange.VIOLATIONS[:]

# --- the live (noisy) market
sim, ex = fresh(quiet=False)
bugs, seen = 0, 0
for i in range(600):
    time.sleep(0.1)
    try:
        seen += len(ex.poll_new_trade_ticks("STKB"))
    except RuntimeError:
        bugs += 1
check("noise takers trade", seen > 20, "%d public ticks in 60 s" % seen)
check("library bug (RuntimeError on poll) shows up now and then", 0 < bugs < 20, "%d times in 600 polls" % bugs)
bid, ask = best(ex, "IDX5_ETF")
check("ETF trades above its theoretical value", (bid.price + ask.price) / 2 - (2.5 + 0.25 * sim.index()) > 0.02)
price_a = sim.price["STKB"]
sim, ex = fresh(quiet=False)
time.sleep(60)
ex.get_positions()
check("same seed gives the same market", sim.price["STKB"] == price_a)

bugs = 0
for i in range(1500):
    try:
        ex.get_trade_history("STKB")
    except RuntimeError:
        bugs += 1
check("get_trade_history can raise it too", 0 < bugs < 20, "%d times in 1500 reads" % bugs)

print("")
print("SELFTEST %s (%d failed)" % ("PASSED" if failures == 0 else "FAILED", failures))
sys.exit(1 if failures else 0)
