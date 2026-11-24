# Virtual clock. After install(), time.time / time.sleep / time.monotonic / time.perf_counter and
# datetime.datetime.now() all follow a simulated clock, so time.sleep(0.2) returns at once and
# simply moves simulated time forward by 0.2 s. The simulated market reads this clock.
#
# Threads: a bot may run several threads (e.g. a fast helper thread that sleeps a few ms while the main loop sleeps
# ~20 ms). Each sleeping thread gets its own wake-up time, and the clock only jumps to the EARLIEST wake-up once every
# thread is asleep, as real time would. Computing takes no simulated time.
import datetime as _datetime
import threading as _threading
import time as _time

REAL_TIME = _time.time
REAL_PERF_COUNTER = _time.perf_counter
REAL_DATETIME = _datetime.datetime
START = REAL_DATETIME(2026, 11, 10, 18, 30, 0)          # virtual start (arbitrary), naive UTC
START_EPOCH = START.replace(tzinfo=_datetime.timezone.utc).timestamp()
GRACE_SECONDS = 60.0     # how long a bot may keep running after the stop signal

elapsed = 0.0            # simulated seconds since START
sleep_count = 0          # number of time.sleep calls so far (used to spot busy loops)
deadline = None          # simulated second at which the runner stops the bot (None = never)
interrupt_sent = False
installed = False


class MockHardStop(BaseException):
    """Raised when a bot ignores the stop signal for more than GRACE_SECONDS."""


def advance(seconds):
    """Move simulated time forward. Raises KeyboardInterrupt once when the deadline passes,
    exactly as if the user pressed Ctrl-C, so the bot's own shutdown code runs."""
    global elapsed, interrupt_sent
    elapsed += seconds
    if deadline is None or elapsed < deadline:
        return
    if not interrupt_sent:
        interrupt_sent = True
        raise KeyboardInterrupt("mock: simulated run time is over")
    if elapsed >= deadline + GRACE_SECONDS:
        raise MockHardStop("mock: bot was still running %.0f s after the stop signal" % GRACE_SECONDS)


_cond = _threading.Condition()
API_LOCK = _threading.RLock()   # one exchange call at a time (synchronous_client.py)
_sleepers = {}           # thread ident -> simulated wake-up second
_threads = {}            # thread ident -> Thread, every thread that has slept at least once
_for_main = []           # stop signals raised by another thread's advance, delivered to the main thread
STUCK_SECONDS = 0.2      # real seconds a sleeper waits for a busy (or blocked) thread before moving on anyway


def _move_to(target):
    """Advance to `target`. A stop signal is raised here only on the main thread, otherwise kept for it."""
    if target <= elapsed:
        return
    try:
        advance(target - elapsed)
    except (KeyboardInterrupt, MockHardStop) as signal:
        if _threading.current_thread() is _threading.main_thread():
            raise
        _for_main.append(signal)


def virtual_sleep(seconds):
    global sleep_count
    if seconds < 0:
        raise ValueError("sleep length must be non-negative")
    sleep_count += 1
    me = _threading.current_thread()
    is_main = me is _threading.main_thread()
    with _cond:
        _threads[me.ident] = me
        target = elapsed + float(seconds)
        _sleepers[me.ident] = target
        try:
            while True:
                if is_main and _for_main:
                    raise _for_main.pop(0)
                if elapsed >= target:
                    return
                for ident, thread in list(_threads.items()):
                    if not thread.is_alive():
                        del _threads[ident]
                if all(ident in _sleepers for ident in _threads):
                    soonest = min(_sleepers.values())
                    if soonest > elapsed:
                        _move_to(soonest)                  # everyone asleep: jump to the earliest wake-up
                        _cond.notify_all()
                        continue
                    _cond.notify_all()                     # another thread is due now: let it run first
                if not _cond.wait(timeout=STUCK_SECONDS):
                    _move_to(target)                       # another thread is stuck: don't hang the run
                    _cond.notify_all()
        finally:
            _sleepers.pop(me.ident, None)
            _cond.notify_all()


def virtual_time():
    return START_EPOCH + elapsed


def virtual_monotonic():
    return 1000.0 + elapsed


class _InstanceCheck(type):
    # Makes isinstance(x, datetime.datetime) still true for datetimes created before install().
    def __instancecheck__(cls, obj):
        return isinstance(obj, REAL_DATETIME)


class VirtualDateTime(REAL_DATETIME, metaclass=_InstanceCheck):
    """datetime.datetime whose now()/utcnow()/today() read the virtual clock."""

    @classmethod
    def now(cls, tz=None):
        naive_utc = _START_VIRTUAL + _datetime.timedelta(seconds=elapsed)
        if tz is None:
            return naive_utc
        return naive_utc.replace(tzinfo=_datetime.timezone.utc).astimezone(tz)

    @classmethod
    def utcnow(cls):
        return cls.now()

    @classmethod
    def today(cls):
        return cls.now()


_START_VIRTUAL = VirtualDateTime(2026, 11, 10, 18, 30, 0)


def set_start_ns(ns):
    """Replay mode: make the virtual clock start at a real moment (UTC, nanoseconds since 1970)."""
    global START, START_EPOCH, _START_VIRTUAL
    START = REAL_DATETIME.fromtimestamp(ns / 1e9, _datetime.timezone.utc).replace(tzinfo=None)
    START_EPOCH = ns / 1e9 - elapsed
    _START_VIRTUAL = VirtualDateTime(*START.timetuple()[:6], START.microsecond) - _datetime.timedelta(seconds=elapsed)


def install(run_seconds=None):
    """Switch the whole process to virtual time. Safe to call more than once."""
    global installed, deadline
    if run_seconds is not None:
        deadline = elapsed + run_seconds
    if installed:
        return
    installed = True
    _time.time = virtual_time
    _time.sleep = virtual_sleep
    _time.monotonic = virtual_monotonic
    _time.perf_counter = virtual_monotonic
    _datetime.datetime = VirtualDateTime
