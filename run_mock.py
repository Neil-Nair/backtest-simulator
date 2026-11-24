# Runs a bot against the offline simulated exchange on virtual time, then prints a REPORT.
#   python3 run_mock.py <script.py> --seconds N [--seed S] [--start-positions JSON] [--poll-drop P]
#                       [--position-lag SEC] [--quiet] [-- script args]
# Exit code 1 if the bot broke a rule or crashed, otherwise 0.
import argparse
import io
import os
import runpy
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    argv = sys.argv[1:]
    script_args = []
    if "--" in argv:                                   # everything after "--" belongs to the bot
        split = argv.index("--")
        argv, script_args = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description="Run a bot against the mock exchange.")
    parser.add_argument("script")
    parser.add_argument("--seconds", type=float, default=60.0, help="simulated seconds to run")
    parser.add_argument("--seed", type=int, default=None, help="market seed (default: env MOCK_SEED or 1)")
    parser.add_argument("--start-positions", default=None, help='json, e.g. {"IDX5_ETF": -100}')
    parser.add_argument("--poll-drop", type=float, default=None,
                        help="fraction 0..1 of the fills in a burst that poll_new_trades never reports "
                             "(default 0.08; env MOCK_POLL_DROP). get_trade_history is always complete")
    parser.add_argument("--position-lag", type=float, default=None,
                        help="stress option: seconds the position number lags behind fills of RESTING orders "
                             "(default 0 = exact and instant; env MOCK_POSITION_LAG)")
    parser.add_argument("--quiet", action="store_true", help="hide the bot's own printing")
    args = parser.parse_args(argv)

    if args.seed is not None:
        os.environ["MOCK_SEED"] = str(args.seed)
    if args.start_positions is not None:
        os.environ["MOCK_START_POSITIONS"] = args.start_positions
    if args.position_lag is not None:
        os.environ["MOCK_POSITION_LAG"] = str(args.position_lag)
    if args.poll_drop is not None:
        os.environ["MOCK_POLL_DROP"] = str(args.poll_drop)
    script = os.path.abspath(args.script)
    # This repo's mockexchange first; the bot's own folder next, for sibling imports. The bot runs from its folder.
    sys.path[0:0] = [ROOT, os.path.dirname(script)]
    os.chdir(os.path.dirname(script))
    sys.argv = [script] + script_args

    import mockexchange
    from mockexchange import _sim, _vclock
    real_start = _vclock.REAL_PERF_COUNTER()
    _vclock.install(run_seconds=args.seconds)

    real_stdout = sys.stdout
    if args.quiet:
        sys.stdout = io.StringIO()
    how_it_ended, crashed = "script finished by itself", False
    try:
        runpy.run_path(script, run_name="__main__")
    except KeyboardInterrupt:
        how_it_ended = "stopped by the simulated Ctrl-C (bot did not catch it)"
    except SystemExit as stop:
        how_it_ended = "script called exit(%r)" % (stop.code,)
        crashed = stop.code not in (None, 0)
    except _vclock.MockHardStop as stop:
        how_it_ended, crashed = str(stop), True
    except BaseException:
        how_it_ended, crashed = "CRASHED:\n" + traceback.format_exc(), True
    finally:
        sys.stdout = real_stdout
    if _vclock.interrupt_sent and how_it_ended == "script finished by itself":
        how_it_ended = "stopped cleanly after the simulated Ctrl-C"
    _vclock.deadline = None

    print("")
    print("=" * 30 + " MOCK REPORT " + "=" * 30)
    print("script: %s" % script)
    print("ended: %s" % how_it_ended)
    sim = _sim.CURRENT
    if sim is None:
        print("The script never created an Exchange().")
        return 1 if crashed else 0
    sim.advance()
    print("simulated %.1f s in %.1f s of real time, seed %s" % (
        sim.now(), _vclock.REAL_PERF_COUNTER() - real_start, os.environ.get("MOCK_SEED", "1")))
    if getattr(sim, "replay", None) is not None:
        print(sim.replay.summary())
    print("FINAL PNL (cash + positions at last traded price): %+.2f" % sim.pnl())
    print("          (positions valued at the hidden fair value instead: %+.2f)" % sim.pnl(use_fair=True))
    print("%-20s %8s %12s %7s %7s" % ("instrument", "position", "pnl", "fills", "lots"))
    for name in sorted(sim.books):
        book = sim.books[name]
        if book.position or book.fill_count or abs(book.cash) > 1e-9:
            print("%-20s %8d %+12.2f %7d %7d" % (name, book.position, book.cash + book.position * book.last_price,
                                                  book.fill_count, book.fill_lots))
    print("own fills in total: %d fills, %d lots" % (sum(b.fill_count for b in sim.books.values()),
                                                     sum(b.fill_lots for b in sim.books.values())))
    print("fills poll_new_trades never reported (a bot counting them is wrong by this): %d fills" % (
        sum(b.poll_lost for b in sim.books.values())))
    print("net delta per group: %s" % ({g: round(d, 1) for g, d in sim.group_deltas().items()} or "flat"))
    print("order messages: %d in total, at most %d in any rolling second (limit %d)" % (
        sim.message_total, sim.message_max, _sim.RATE_LIMIT))
    resting = {n: sum(o.volume for o in b.own["bid"] + b.own["ask"]) for n, b in sim.books.items()}
    resting = {n: v for n, v in resting.items() if v}
    print("orders left resting at the end: %s" % (resting or "none"))
    if sim.amend_ups:
        print("amends that ENLARGED an order (a fill raced the amend; allowed, but never intended): %d" % sim.amend_ups)
    print("VIOLATIONS: %d" % len(mockexchange.VIOLATIONS))
    for when, text in mockexchange.VIOLATIONS:
        print("  t=%8.2f s  %s" % (when, text))
    print("RESULT: %s" % ("FAIL" if (crashed or mockexchange.VIOLATIONS) else "OK"))
    return 1 if (crashed or mockexchange.VIOLATIONS) else 0


if __name__ == "__main__":
    sys.exit(main())
