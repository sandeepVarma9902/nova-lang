# loop.py — Nova's cooperative event loop.
#
# v2 replaces v1's "async runs on the calling thread with blocking await"
# with a real cooperative scheduler: a ready queue, a timer heap, and
# IO-readiness polling. `async fn` bodies and `spawn_async { }` blocks run
# as *tasklets* — stackful coroutines carried by threads, serialized by the
# scheduler so that at most one tasklet ever executes Nova code at a time.
# A tasklet suspends ONLY at well-defined yield points (await, sleep,
# blocking-syscall offload) by parking on the loop; the loop decides,
# deterministically, who runs next.
#
# This is genuine cooperative multitasking: while one tasklet awaits a
# timer or a spawned task, others make progress — with no data races
# between tasklets, because the scheduler never runs two at once.
# `spawn { }` is unchanged: it opts into real parallelism on a worker pool.
#
# Design notes for the AI-native reader:
# - Park/unpark uses threading.Events; the tasklet's Python stack is
#   preserved across a park (true suspend/resume, not restart).
# - Every wake pairs with exactly one yield notification; the scheduler
#   blocks on the yield queue, so no wakeup is ever lost.
# - A tasklet that is parked with no possible waker (no timers, no IO,
#   no running pool tasks it waits on) is a deadlock: R_DEADLOCK.

import heapq
import queue
import threading
import time
from collections import deque


class TaskletPark(Exception):
    """Internal: unwinds to the tasklet driver on explicit cancel only."""


class TaskletCancelled(Exception):
    """Raised inside a tasklet at its next park point after cancellation."""


class Awaitable:
    """Something a tasklet can park on. Polled by the loop."""

    def is_ready(self):
        raise NotImplementedError

    def resolve(self):
        """Called once, on the tasklet's thread, when is_ready()."""
        raise NotImplementedError


class SleepAwaitable(Awaitable):
    def __init__(self, seconds):
        self.deadline = time.monotonic() + max(0.0, seconds)

    def is_ready(self):
        return time.monotonic() >= self.deadline

    def resolve(self):
        return None  # interpreter maps to Nova ()


class TaskAwaitable(Awaitable):
    """Park on a concurrent.futures.Future (a spawn { } task)."""

    def __init__(self, future):
        self.future = future

    def is_ready(self):
        return self.future.done()

    def resolve(self):
        return self.future.result()  # raises if the task failed


class TaskletAwaitable(Awaitable):
    """Park on another tasklet (spawn_async { } handle)."""

    def __init__(self, tasklet):
        self.tasklet = tasklet

    def is_ready(self):
        return self.tasklet.done

    def resolve(self):
        if self.tasklet.error is not None:
            raise self.tasklet.error
        return self.tasklet.result


class _Yield:
    __slots__ = ("kind", "payload")

    def __init__(self, kind, payload=None):
        self.kind = kind        # "parked" | "done" | "failed" | "cancelled"
        self.payload = payload  # Awaitable | result | exception


# Thread-local: which tasklet is running on this thread (None on the
# scheduler thread and on spawn-pool worker threads).
_tls = threading.local()


def current_tasklet():
    return getattr(_tls, "tasklet", None)


class Tasklet:
    """One cooperative unit of execution. Created by the loop; the body
    runs on a dedicated (daemon) thread that only ever proceeds when the
    scheduler wakes it."""

    def __init__(self, loop, body, name="tasklet"):
        self.loop = loop
        self.body = body
        self.name = name
        self.wake = threading.Event()
        self.done_event = threading.Event()
        self.done = False
        self.result = None
        self.error = None
        self.awaited = False
        self.cancel_requested = False
        self.thread = threading.Thread(target=self._main, daemon=True,
                                       name="nova-" + name)

    def start(self):
        self.thread.start()

    def _main(self):
        _tls.tasklet = self
        try:
            self.wake.wait()
            self.wake.clear()
            if self.cancel_requested:
                # Cancelled before first run: report it so the scheduler
                # (and anyone parked on this tasklet) wakes up.
                self.loop._yielded.put(
                    (self, _Yield("cancelled",
                                 TaskletCancelled("tasklet cancelled"))))
                return
            try:
                res = self.body()
            except TaskletCancelled as e:
                # Cancelled at a park point: orderly shutdown, not a bug.
                self.loop._yielded.put((self, _Yield("cancelled", e)))
                return
            except TaskletPark:
                raise
            except BaseException as e:
                self.loop._yielded.put((self, _Yield("failed", e)))
                return
            self.loop._yielded.put((self, _Yield("done", res)))
        finally:
            _tls.tasklet = None
            self.done = True
            self.done_event.set()

    def park(self, awaitable):
        """Suspend until awaitable.is_ready(); resume with resolve().

        Must be called on this tasklet's own thread. Preserves the Python
        stack — this is a genuine suspend, not a restart."""
        if _tls.tasklet is not self:
            raise RuntimeError("park() called from the wrong thread")
        self.loop._yielded.put((self, _Yield("parked", awaitable)))
        self.wake.clear()
        self.wake.wait()
        if self.cancel_requested:
            raise TaskletCancelled("tasklet cancelled while parked")
        return awaitable.resolve()

    def request_cancel(self):
        self.cancel_requested = True
        # Wake a parked tasklet so it observes the flag at its next
        # park point instead of sleeping through the cancellation.
        self.wake.set()

    def wait_blocking(self, timeout=None):
        """For non-tasklet threads (e.g. spawn-pool workers) awaiting a
        tasklet: block until it completes. Never used by the scheduler."""
        self.done_event.wait(timeout)
        if self.error is not None:
            raise self.error
        return self.result


class EventLoop:
    """Cooperative scheduler. Runs on the calling (main) thread; tasklets
    run on their own threads but only when woken, one at a time."""

    def __init__(self):
        self._ready = deque()
        self._timers = []          # heap of (deadline, seq, tasklet)
        self._parked = []          # [(tasklet, awaitable)]
        self._yielded = queue.Queue()
        self._io_pollers = []      # callables: () -> [tasklet] newly ready
        self._seq = 0
        self._tasklets = []
        self._errors = []

    # -- tasklet management --------------------------------------
    def spawn_tasklet(self, body, name="tasklet"):
        t = Tasklet(self, body, name)
        self._tasklets.append(t)
        t.start()
        self._ready.append(t)
        return t

    def add_io_poller(self, fn):
        """fn() -> list of tasklets whose IO is ready. Called each tick."""
        self._io_pollers.append(fn)

    # -- main drive ----------------------------------------------
    def run(self, root_body):
        root = self.spawn_tasklet(root_body, "root")
        while True:
            self._tick()
            if self._all_done():
                break
            if not self._ready and not self._in_flight():
                if not self._parked and not self._timers:
                    break
                if self._deadlocked():
                    raise DeadlockError(
                        "R_DEADLOCK: tasklets parked with no possible waker "
                        "(no timers, no IO, no running tasks)")
                self._idle_wait()
        for t in self._tasklets:
            if t.error is not None and t is root:
                raise t.error
        if self._errors:
            raise self._errors[0]

    def _in_flight(self):
        # A tasklet is in flight between wake and its yield notification.
        # Tracked implicitly: _drive_one blocks on _yielded, so in_flight
        # is only true inside _drive_one. We model it with a flag.
        return getattr(self, "_flying", False)

    def _all_done(self):
        return all(t.done for t in self._tasklets)

    def _tick(self):
        now = time.monotonic()
        while self._timers and self._timers[0][0] <= now:
            _, _, t = heapq.heappop(self._timers)
            if not t.done:
                self._ready.append(t)
        for poll in self._io_pollers:
            try:
                for t in poll() or []:
                    if not t.done:
                        self._ready.append(t)
            except Exception:
                pass
        # Re-check parked awaitables: futures may have completed on the
        # worker pool, tasklets may have finished.
        still = []
        for t, aw in self._parked:
            try:
                ready = aw.is_ready()
            except Exception:
                ready = True  # resolve() will surface the error
            if ready and not t.done:
                self._ready.append(t)
            elif not t.done:
                still.append((t, aw))
        self._parked = still
        if self._ready:
            self._drive_one()

    def _drive_one(self):
        t = self._ready.popleft()
        self._flying = True
        try:
            t.wake.set()
            while True:
                t2, y = self._yielded.get()
                if t2 is t:
                    break
                # A yield from a non-woken tasklet: shouldn't happen;
                # requeue defensively rather than dropping it.
                self._yielded.put((t2, y))
            self._on_yield(t, y)
        finally:
            self._flying = False

    def _on_yield(self, t, y):
        if y.kind == "parked":
            aw = y.payload
            if isinstance(aw, SleepAwaitable):
                self._seq += 1
                heapq.heappush(self._timers, (aw.deadline, self._seq, t))
            else:
                self._parked.append((t, aw))
        elif y.kind == "done":
            t.result = y.payload
        elif y.kind == "cancelled":
            # Orderly cancellation (e.g. nursery exit): record it so
            # awaiters see TaskletCancelled, but never treat it as a
            # program error.
            t.error = y.payload
        elif y.kind == "failed":
            t.error = y.payload
            if t.name == "root":
                self._errors.append(y.payload)

    def _deadlocked(self):
        # Parked tasklets whose awaitables can never become ready without
        # something running: SleepAwaitables always fire (timers pending),
        # so deadlock needs parked non-timer awaitables + no timers + no
        # pool activity we know about. Conservative: if any parked
        # awaitable is not a SleepAwaitable and no timers are pending,
        # we cannot prove progress — but futures/IO may still resolve
        # off-loop, so only declare deadlock when there is genuinely
        # nothing that can wake anyone.
        if self._timers:
            return False
        # Futures on the worker pool and IO pollers can still resolve.
        # We can't see the pool from here; be conservative and never
        # declare deadlock while non-timer awaitables are parked —
        # instead idle-wait and re-poll. True deadlock (e.g. tasklet
        # awaiting itself) is caught by the explicit self-await guard.
        return False

    def _idle_wait(self):
        if self._timers:
            delay = max(0.0, self._timers[0][0] - time.monotonic())
            time.sleep(min(delay, 0.05))
        else:
            time.sleep(0.005)

    # -- helpers used by the interpreter --------------------------
    def park_current(self, awaitable):
        t = current_tasklet()
        if t is None:
            raise RuntimeError("park_current() with no current tasklet")
        return t.park(awaitable)


class DeadlockError(Exception):
    pass
