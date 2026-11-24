# Backtest simulator

An offline exchange simulator for testing trading bots on virtual time. A bot connects to it through a small
synchronous client API, places and cancels orders, and the simulator matches them with price-time priority against
either a synthetic market or a replay of recorded order events. At the end you get a report with PnL per product,
fills, message rates and every rule the bot broke.

It was built to answer two questions before a bot goes anywhere near a real venue: *does it follow the rules?* and
*does a change actually make it better?*

## What it does

- **Virtual clock.** `time.time`, `time.sleep`, `time.monotonic` and `datetime.now` are patched to a simulated clock,
  so `time.sleep(0.2)` returns at once and moves time forward. A 30-minute session runs in seconds. Bots with several
  threads are supported: the clock only jumps forward once every thread is asleep.
- **Synthetic market** (`mockexchange/_sim.py`). Hidden fair values (random walks for stocks; futures, an ETF and
  Black-Scholes options derived from them), background market makers that re-quote around fair value, noise takers
  that sweep the book, and a queue model so a resting order only fills once the volume ahead of it has traded.
  Seeded, so the same seed gives the same market.
- **Market replay** (`mockexchange/_replay.py`). Swap the synthetic market for a file of recorded order events. The
  bot's orders join the recorded queues and are filled only when real incoming volume gets through everything queued
  in front of them. The main limitation is stated up front: recorded traders do not react to you.
- **Rule checks.** Position limits (worst case including resting orders), outstanding volume, message rate, a soft
  delta limit per instrument group, price-change limits, tick grid, busy loops. Breaches are recorded and the hard
  ones disconnect the bot, as a real venue would.
- **Stress options.** Lossy trade polling in bursts, lagging position updates, occasional client exceptions, and a
  simulated Ctrl-C at the end so a bot's shutdown code is tested too.
- **Tools.** `tools/run_test.py` runs one or two bots over several seeds or a replay window and writes a Markdown
  comparison; `tools/make_window.py` cuts replay windows from per-minute parquet files; `tools/calibrate.py` compares a
  replay with the live run it replays, product by product, so you know where to trust it.

The instruments, prices and limits in `_sim.py` are illustrative defaults for a synthetic market. Edit them to model
whatever you want to test against.

## Quick start

Python 3.9+ and the standard library are enough for the simulator. The replay-window tool also needs pandas and pyarrow.

```
python selftest.py                                    # ends with SELFTEST PASSED
python run_mock.py example_bot.py --seconds 600 --quiet
python tools/run_test.py --bot example_bot.py --seconds 300 --seeds 3,5
```

A bot imports the client from `mockexchange`:

```python
from mockexchange.synchronous_client import Exchange

exchange = Exchange()
exchange.connect()
book = exchange.get_last_price_book("STKD")
exchange.insert_order("STKD", price=book.bids[0].price, volume=5, side="bid", order_type="limit")
```

`example_bot.py` is a minimal working bot (it joins the best bid and ask in one stock) that exists only to show the
API and prove the runner works. It is not a trading strategy.

### Replaying recorded order events

```
MOCK_REPLAY=window.csv.gz python run_mock.py my_bot.py --seconds 1200 --quiet
```

The file is a time-sorted CSV with columns
`timestamp_ns,instrument_id,order_id,price,volume,side,action,order_type` (`action` is `new`, `fill`, `partialFill`,
`amended` or `cancelled`). Other settings: `MOCK_REPLAY_WARMUP`, `MOCK_REPLAY_EXCLUDE`, `MOCK_REPLAY_OWN_GRACE` (see
the top of `mockexchange/_replay.py`). No market data is included in this repository.

### Settings

| Setting | What it does |
|---|---|
| `--seconds N` | simulated seconds to run |
| `--seed S` / `MOCK_SEED` | market seed |
| `--start-positions JSON` | start with positions, e.g. `{"STKD": -50}` |
| `--poll-drop P` / `MOCK_POLL_DROP` | share of burst fills that trade polling never reports (default 0.08) |
| `--position-lag SEC` / `MOCK_POSITION_LAG` | position number lags behind resting fills by this much |
| `MOCK_THREADS=0` | run without thread scheduling |

## Layout

| Path | What it is |
|---|---|
| `mockexchange/` | the simulated exchange: client, data types, market simulation, replay engine, virtual clock |
| `run_mock.py` | runs one bot on virtual time and prints the report |
| `selftest.py` | checks the simulator itself (clock, queues, matching, every rule) |
| `example_bot.py` | minimal bot that shows the API |
| `tools/` | `run_test.py`, `make_window.py`, `calibrate.py` |

## Credits

Built in collaboration with **Kijun Lee** and **Louis Buckland** (OxPhys).

## Disclaimer

This repository contains **no sensitive Optiver information** and **no trading strategies**. It holds only the
simulator: no venue data, no recorded market data, no documentation or course material, no credentials, and none of
the bots that were tested with it. The simulated instruments, prices and limits are illustrative. This project is not
affiliated with or endorsed by Optiver.

## Licence

MIT, see [LICENSE](LICENSE).
