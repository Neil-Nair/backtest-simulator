# make_window.py - cut a replay window out of a folder of order-event parquet files. Read-only.
#
# The replay exchange (MOCK_REPLAY, see mockexchange/_replay.py) needs the order events of one time window, all
# instruments, time-sorted, as CSV. This script makes that file from parquet files with one file per minute, named
# like  <anything>orders<anything>YYYYMMDD_HHMM00<anything>.parquet  and holding the columns
# timestamp_ns, instrument_id, order_id, price, volume, side, action, order_type. Needs: pip install pandas pyarrow
#
# usage:
#   python make_window.py 2026-11-15T20:10:00 2026-11-15T20:25:00 window.csv.gz --source DIR
#   ... --instruments STKA,STKB,STKB_202703_F   (default: all instruments)
# Times are UTC. Start the window a couple of minutes BEFORE the moment you want the bot to start: the replay uses the
# first MOCK_REPLAY_WARMUP seconds (default 120) to fill the order books.
import argparse
import gc
import glob
import os
import re
import sys
from datetime import datetime, timedelta, timezone

COLS = ["timestamp_ns", "instrument_id", "order_id", "price", "volume", "side", "action", "order_type"]
HERE = os.path.dirname(os.path.abspath(__file__))


def utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def minutes(a, b):
    t = a.replace(second=0, microsecond=0)
    while t < b:
        yield t
        t += timedelta(minutes=1)


def main():
    p = argparse.ArgumentParser(description="Cut a replay window out of a folder of order-event parquet files.")
    p.add_argument("start"), p.add_argument("end"), p.add_argument("out")
    p.add_argument("--instruments", default="", help="comma list; default all")
    p.add_argument("--source", default=os.path.join(HERE, "..", "data"), help="folder of per-minute parquet files")
    args = p.parse_args()
    a, b = utc(args.start), utc(args.end)
    want = set(x for x in args.instruments.split(",") if x)
    a_ns, b_ns = int(a.timestamp() * 1e9), int(b.timestamp() * 1e9)
    import pyarrow.parquet as pq
    files = {}
    for f in glob.glob(os.path.join(args.source, "*orders*.parquet")):
        hit = re.search(r"(\d{8})_(\d{4})00", os.path.basename(f))
        if hit:
            files[datetime.strptime(hit.group(1) + hit.group(2), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)] = f
    missing = [m for m in minutes(a, b) if m not in files]
    if missing:
        sys.exit("missing minutes in %s: %s ... (%d of them). Pick another window."
                 % (args.source, ", ".join(m.strftime("%d %b %H:%M") for m in missing[:5]), len(missing)))
    paths = [files[m] for m in minutes(a, b)]

    def chunks():
        """Small batches, filtered at once, so memory stays low however big a file is."""
        for path in sorted(set(paths)):
            pf = pq.ParquetFile(path)
            names = pf.schema_arrow.names
            for g in range(pf.metadata.num_row_groups):
                st = pf.metadata.row_group(g).column(names.index("timestamp_ns")).statistics
                if st is not None and st.has_min_max and (st.max < a_ns or st.min >= b_ns):
                    continue
                for batch in pf.iter_batches(batch_size=200000, row_groups=[g], columns=COLS):
                    yield batch.to_pandas()

    first, n, last, unsorted = True, 0, 0, 0
    for d in chunks():
        if d is None or not len(d):
            continue
        d = d[(d.timestamp_ns >= a_ns) & (d.timestamp_ns < b_ns)]
        if want:
            d = d[d.instrument_id.isin(want)]
        if not len(d):
            continue
        d = d.sort_values("timestamp_ns", kind="stable")[COLS].assign(price=lambda x: x.price.round(4))
        unsorted += int(d.timestamp_ns.iloc[0] < last)
        last = int(d.timestamp_ns.iloc[-1])
        d.to_csv(args.out, index=False, mode="w" if first else "a", header=first,
                 compression="gzip" if args.out.endswith(".gz") else None)
        first, n = False, n + len(d)
        del d
        gc.collect()
    print("%d order events %s - %s -> %s" % (n, a.strftime("%d %b %H:%M:%S"), b.strftime("%H:%M:%S"), args.out))
    if unsorted:
        sys.exit("WARNING: %d batches started earlier than the previous one ended: the file is not time-sorted" % unsorted)


if __name__ == "__main__":
    main()
