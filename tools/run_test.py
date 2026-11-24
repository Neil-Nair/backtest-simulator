# run_test.py - one command for an offline test: runs one bot (and optionally a second one to compare) on the simulated
# market (rule check, several seeds) or on a replay of a recorded market, then writes a short report (summary.md) with
# the PnL per product side by side.
#
# usage (from the repo's top folder):
#   python tools/run_test.py --bot example_bot.py --seconds 300
#   python tools/run_test.py --bot my_idea.py --compare example_bot.py --market path/to/window.csv.gz --seconds 1200
#   --market invented            the simulated market, once per seed (--seeds 3,5): rule check, quick
#   --market path/to/window.csv.gz   a replay window (see tools/make_window.py)
#   --exclude path/to/fills.csv  with a replay: the live fills of a bot that traded in that window (MOCK_REPLAY_EXCLUDE)
#   --bot-args "--flatten"       extra options passed to the bot(s)
#   --out DIR                    where the logs, fills and summary.md go (default results/)
import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def market_env(market, exclude=""):
    if market == "invented":
        return {}, "invented market"
    path = os.path.abspath(market)
    if not os.path.exists(path):
        sys.exit("no such market or window file: %s" % market)
    env = {"MOCK_REPLAY": path}
    if exclude:
        env["MOCK_REPLAY_EXCLUDE"] = os.path.abspath(exclude)
    return env, "replay of %s" % os.path.basename(path)


def run_one(bot, env_extra, seconds, seed, bot_args, out, tag):
    bot = os.path.abspath(bot)
    if not os.path.exists(bot):
        sys.exit("no such bot file: %s" % bot)
    # the bot runs from its own folder with THIS repo's simulated exchange
    env = dict(os.environ, MOCK_SEED=str(seed), PYTHONUNBUFFERED="1", **env_extra)
    fills = os.path.join(out, tag + "_fills.csv")
    log = os.path.join(out, tag + ".log")
    cmd = [sys.executable, os.path.join(ROOT, "run_mock.py"), bot, "--seconds", str(seconds), "--seed", str(seed),
           "--quiet", "--"] + bot_args + (["--fills-csv", fills] if "fills-csv" in open(bot).read() else [])
    t0 = time.time()
    with open(log, "w") as f:
        code = subprocess.call(cmd, cwd=os.path.dirname(bot), env=env, stdout=f, stderr=subprocess.STDOUT)
    text = open(log).read()
    report = text[text.find("MOCK REPORT"):] if "MOCK REPORT" in text else text[-3000:]
    res = {"tag": tag, "bot": os.path.relpath(bot, ROOT), "seed": seed, "code": code, "real": time.time() - t0,
           "log": log, "pnl": None, "violations": None, "result": "?", "peak": "?", "per": {}, "ended": "?"}
    m = re.search(r"FINAL PNL[^:]*: ([+-][\d.]+)", report)
    res["pnl"] = float(m.group(1)) if m else None
    m = re.search(r"VIOLATIONS: (\d+)", report)
    res["violations"] = int(m.group(1)) if m else None
    m = re.search(r"RESULT: (\S+)", report)
    res["result"] = m.group(1) if m else "no report (crashed? see the log)"
    m = re.search(r"at most (\d+) in any rolling second", report)
    res["peak"] = m.group(1) if m else "?"
    m = re.search(r"ended: (.*)", report)
    res["ended"] = m.group(1) if m else "?"
    for line in report.splitlines():
        m = re.match(r"(\S+)\s+(-?\d+)\s+([+-][\d.]+)\s+(\d+)\s+(\d+)$", line.strip())
        if m:
            res["per"][m.group(1)] = (float(m.group(3)), int(m.group(5)))
    return res


def main():
    p = argparse.ArgumentParser(description="Offline test of one bot, optionally against a second one.")
    p.add_argument("--bot", required=True)
    p.add_argument("--compare", default="")
    p.add_argument("--market", default="invented")
    p.add_argument("--seconds", type=float, default=300)
    p.add_argument("--seeds", default="3,5")
    p.add_argument("--bot-args", default="")
    p.add_argument("--exclude", default="")
    p.add_argument("--out", default=os.path.join(ROOT, "results"))
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    env, label = market_env(a.market, a.exclude)
    seeds = [int(s) for s in a.seeds.split(",") if s.strip()] if a.market == "invented" else [1]
    bots = [("A", a.bot)] + ([("B", a.compare)] if a.compare else [])
    runs = []
    for name, bot in bots:
        for seed in seeds:
            tag = "%s_%s_seed%d" % (name, os.path.splitext(os.path.basename(bot))[0], seed)
            print("running %s on the %s, %g s, seed %d ..." % (bot, label, a.seconds, seed), flush=True)
            r = run_one(bot, env, a.seconds, seed, a.bot_args.split(), a.out, tag)
            r["name"] = name
            runs.append(r)
            print("  -> PnL %s, violations %s, %s (%.0f s real)" % (r["pnl"], r["violations"], r["result"], r["real"]),
                  flush=True)

    hours = a.seconds / 3600.0
    md = ["# Offline test: %s" % label, "",
          "%g simulated seconds%s. PnL per hour = PnL / %.3f h." % (
              a.seconds, "" if a.market != "invented" else ", seeds " + a.seeds, hours), ""]
    md += ["| Bot | Seed | PnL | PnL per hour | Rule breaches | Peak msgs/s | Result |", "|---|---|---|---|---|---|---|"]
    for r in runs:
        md.append("| %s `%s` | %s | %s | %s | %s | %s | **%s** |" % (
            r["name"], r["bot"], r["seed"] if a.market == "invented" else "-",
            "%+.0f" % r["pnl"] if r["pnl"] is not None else "?",
            "%+.0f" % (r["pnl"] / hours) if r["pnl"] is not None else "?",
            r["violations"], r["peak"], r["result"]))
    if a.market != "invented":
        md += ["", "**Trust:** only products where the replay was calibrated against a live run (tools/calibrate.py) "
               "count as evidence; compare two bots on the same window rather than reading one absolute number."]
    # per product, first seed of each bot
    firsts = {}
    for r in runs:
        firsts.setdefault(r["name"], r)
    names = sorted(set().union(*(r["per"] for r in firsts.values()))) if firsts else []
    if names:
        cols = list(firsts)
        md += ["", "## PnL per product (lots traded in brackets)%s" % ("" if a.market != "invented" else ", first seed"), "",
               "| Product | " + " | ".join("%s" % c for c in cols) + (" | B - A |" if len(cols) == 2 else " |"),
               "|---|" + "---|" * (len(cols) + (1 if len(cols) == 2 else 0))]
        for n in names:
            vals = [firsts[c]["per"].get(n, (0.0, 0)) for c in cols]
            row = "| %s | " % n + " | ".join("%+.0f (%d)" % v for v in vals)
            if len(cols) == 2:
                row += " | %+.0f" % (vals[1][0] - vals[0][0])
            md.append(row + " |")
    md += ["", "Logs and fills: the files next to this summary (`*.log`, `*_fills.csv`)."]
    path = os.path.join(a.out, "summary.md")
    open(path, "w").write("\n".join(md) + "\n")
    print("\n".join(md))
    print("\nsummary written to %s" % path)
    bad = [r for r in runs if r["result"] != "OK"]
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
