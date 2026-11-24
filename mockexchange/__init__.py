# mockexchange: an offline simulated exchange with a synchronous trading-client API.
# The market behind the client is a small simulation (see _sim.py) or a replay of recorded order events
# (see _replay.py), driven by a virtual clock (see _vclock.py).

# Every rule a bot breaks is appended here as (simulated seconds since start, description).
VIOLATIONS = []

# The virtual clock runs a bot's own threads on time, so bots that start threads can be tested too.
# MOCK_THREADS=0 = the simpler behaviour (bots are expected to run everything in their main loop).
import os as _os
THREADS_OK = _os.environ.get("MOCK_THREADS", "1") != "0"
