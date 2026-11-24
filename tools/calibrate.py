# calibrate.py - how close is a replay to the live run it replays? Reads files only.
#
# Run the SAME bot version on the replay of a window in which it really traded live, with --fills-csv, then compare the
# two fill logs product by product: lots traded, how many of them were resting (passive) fills, and the edge per lot
# (how far the fill price was from the mid-price at that moment, in our favour). If the replay matches live closely for
# a product, its numbers for a NEW strategy in that product can be trusted about as far; where it doesn't, they can't.
#
# usage: python calibrate.py LIVE_FILLS.csv[.gz] REPLAY_FILLS.csv [--minutes-live 30.2 --minutes-replay 28]
#   LIVE_FILLS   = the bot's fills file from the live run (columns instrument, side, price, volume, mid_at_fill, ioc)
#   REPLAY_FILLS = the --fills-csv file of the replay run
# Lots are scaled to per hour, so a replay shorter than the live round still compares.
import argparse
import csv
import gzip
import io
from collections import defaultdict


def read(path):
    f = io.TextIOWrapper(gzip.open(path, "rb"), newline="") if path.endswith(".gz") else open(path, newline="")
    out = defaultdict(lambda: {"lots": 0, "passive": 0, "edge": 0.0})
    with f:
        for r in csv.DictReader(f):
            try:
                lots, price, mid = int(float(r["volume"])), float(r["price"]), float(r["mid_at_fill"] or "nan")
            except (KeyError, ValueError):
                continue
            sign = 1 if r["side"] == "bid" else -1
            row = out[r["instrument"]]
            row["lots"] += lots
            if str(r.get("ioc", "0")) in ("0", "False", ""):
                row["passive"] += lots
            if mid == mid:                                   # not NaN
                row["edge"] += sign * (mid - price) * lots   # bought below mid / sold above mid = positive
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("live"), p.add_argument("replay")
    p.add_argument("--minutes-live", type=float, default=30.0)
    p.add_argument("--minutes-replay", type=float, default=30.0)
    a = p.parse_args()
    live, rep = read(a.live), read(a.replay)
    kl, kr = 60.0 / a.minutes_live, 60.0 / a.minutes_replay
    print("%-18s %10s %10s %7s %9s %9s %9s %9s" % ("instrument", "lots/h", "lots/h", "ratio", "passive%", "passive%",
                                                   "edge c/lot", "edge c/lot"))
    print("%-18s %10s %10s %7s %9s %9s %9s %9s" % ("", "LIVE", "REPLAY", "rep/live", "LIVE", "REPLAY", "LIVE", "REPLAY"))
    tot = [0, 0]
    for name in sorted(set(live) | set(rep)):
        L, R = live.get(name, {"lots": 0, "passive": 0, "edge": 0}), rep.get(name, {"lots": 0, "passive": 0, "edge": 0})
        ll, rl = L["lots"] * kl, R["lots"] * kr
        tot[0] += ll; tot[1] += rl
        print("%-18s %10.0f %10.0f %7s %8.0f%% %8.0f%% %9.2f %9.2f" % (
            name, ll, rl, ("%.2f" % (rl / ll)) if ll else "-", 100 * L["passive"] / max(1, L["lots"]),
            100 * R["passive"] / max(1, R["lots"]), 100 * L["edge"] / max(1, L["lots"]), 100 * R["edge"] / max(1, R["lots"])))
    print("%-18s %10.0f %10.0f %7s" % ("TOTAL", tot[0], tot[1], ("%.2f" % (tot[1] / tot[0])) if tot[0] else "-"))
    print("Read: ratio near 1.00 and similar edge = the replay reproduces that product; far from 1 = don't trust it there.")


if __name__ == "__main__":
    main()
