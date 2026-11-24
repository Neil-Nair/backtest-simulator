# Tiny example bot: keeps a 5-lot bid and a 5-lot ask resting in one stock, joining the best
# prices of the other participants. It only exists to prove that run_mock.py works.
#   python run_mock.py example_bot.py --seconds 120
import sys
import time

from mockexchange.synchronous_client import Exchange

STOCK = sys.argv[1] if len(sys.argv) > 1 else "STKD"
LOTS = 5
MAX_POSITION = 20          # stop quoting a side once we hold this much

exchange = Exchange()
exchange.connect()
tick_size = exchange.get_instruments()[STOCK].tick_size


def to_ticks(price):
    # prices come back as floats like 12.530000000000001, so compare them as whole ticks
    return round(price / tick_size)


def quote_side(side, levels, position, my_orders):
    """Keep one order of LOTS on this side at the best price that is not our own."""
    mine = [o for o in my_orders.values() if o.side == side]
    my_lots = {}
    for order in mine:
        my_lots[to_ticks(order.price)] = my_lots.get(to_ticks(order.price), 0) + order.volume
    # best price of the OTHERS: skip levels that consist only of our own volume
    target = None
    for level in levels:
        if level.volume > my_lots.get(to_ticks(level.price), 0):
            target = to_ticks(level.price)
            break
    room = MAX_POSITION - position if side == "bid" else MAX_POSITION + position
    wanted = target is not None and room >= LOTS
    for order in mine:
        if not wanted or to_ticks(order.price) != target:
            exchange.delete_order(STOCK, order_id=order.order_id)     # price is wrong: remove it
            return                                                    # insert on the next pass
    if wanted and not mine:
        response = exchange.insert_order(STOCK, price=target * tick_size, volume=LOTS, side=side, order_type="limit")
        if not response.success:
            print("insert failed:", response.error_reason)


try:
    while True:
        time.sleep(0.2)                      # never loop without sleeping
        book = exchange.get_last_price_book(STOCK)
        if not book or not book.bids or not book.asks:
            continue
        position = exchange.get_positions().get(STOCK, 0)
        orders = exchange.get_outstanding_orders(STOCK)
        quote_side("bid", book.bids, position, orders)
        quote_side("ask", book.asks, position, orders)
        try:
            for trade in exchange.poll_new_trades(STOCK):
                print("FILL %s %d @ %.2f, pnl %.2f" % (trade.side, trade.volume, trade.price, exchange.get_pnl()))
        except RuntimeError:
            pass                             # simulated client hiccup; the trades come back on the next poll
except KeyboardInterrupt:
    exchange.delete_orders(STOCK)
    print("stopped; position", exchange.get_positions().get(STOCK, 0))
