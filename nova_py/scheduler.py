# scheduler.py — v2 cron scheduler for `nova deploy --target local`.
#
# `resource cron "name" { schedule = "0 2 * * *", run = "report.nova" }`
# in app.nova becomes a real scheduled runner: this module parses the
# 5-field cron expression, and fires the job (as a `nova run` subprocess)
# when the wall clock matches. Stdlib only (threading, subprocess,
# datetime).
#
# Honest limits: minute granularity (standard cron); the scheduler runs
# as long as the local deploy process lives — it is not a system daemon
# (use systemd/launchd or the cloud targets for that).

import datetime
import os
import subprocess
import sys
import threading
import time


def parse_field(text, lo, hi):
    """Parse one cron field into a set of ints. Supports *, */n, a,b,
    a-b, a-b/n, and single values."""
    result = set()
    text = text.strip()
    if text == "*":
        return set(range(lo, hi + 1))
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/", 1)
            step = int(step)
        if part == "*" or part == "":
            a, b = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
        else:
            a = b = int(part)
        if not (lo <= a <= hi and lo <= b <= hi):
            raise ValueError("cron field %r out of range %d-%d"
                             % (text, lo, hi))
        result.update(range(a, b + 1, step))
    return result


class Schedule:
    """A parsed 5-field cron schedule: minute hour dom month dow."""

    def __init__(self, expr):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError("cron schedule needs 5 fields, got %r" % expr)
        self.minutes = parse_field(fields[0], 0, 59)
        self.hours = parse_field(fields[1], 0, 23)
        self.dom = parse_field(fields[2], 1, 31)
        self.months = parse_field(fields[3], 1, 12)
        self.dow = parse_field(fields[4], 0, 6)  # 0=Sunday
        self.expr = expr

    def matches(self, dt):
        # Python weekday(): Mon=0..Sun=6; cron: Sun=0..Sat=6.
        cron_dow = (dt.weekday() + 1) % 7
        return (dt.minute in self.minutes
                and dt.hour in self.hours
                and dt.day in self.dom
                and dt.month in self.months
                and cron_dow in self.dow)

    def next_after(self, dt):
        """Next datetime strictly after dt matching the schedule.
        Scans forward minute by minute (max ~4 years out)."""
        cand = dt.replace(second=0, microsecond=0) \
            + datetime.timedelta(minutes=1)
        for _ in range(525600 * 4):
            if self.matches(cand):
                return cand
            cand += datetime.timedelta(minutes=1)
        raise ValueError("no matching time in 4 years for %r" % self.expr)


class CronJob:
    def __init__(self, name, schedule_expr, run_file, appdir):
        self.name = name
        self.schedule = Schedule(schedule_expr)
        self.run_file = (run_file if os.path.isabs(run_file)
                         else os.path.join(appdir, run_file))
        self.appdir = appdir
        self.last_run_minute = None
        self.lock = threading.Lock()
        self.runs = 0
        self.failures = 0

    def due(self, now):
        minute_key = now.replace(second=0, microsecond=0)
        with self.lock:
            if self.last_run_minute == minute_key:
                return False
            if self.schedule.matches(now):
                self.last_run_minute = minute_key
                return True
            return False

    def fire(self):
        """Run the job file as a subprocess (isolated interpreter)."""
        with self.lock:
            self.runs += 1
        nova = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "nova")
        cmd = [sys.executable, os.path.abspath(nova), "run", self.run_file]
        try:
            p = subprocess.run(cmd, cwd=self.appdir, capture_output=True,
                               text=True, timeout=600)
            if p.returncode != 0:
                with self.lock:
                    self.failures += 1
                sys.stderr.write(
                    "[cron %s] failed (exit %d): %s\n"
                    % (self.name, p.returncode, p.stderr.strip()[-500:]))
            else:
                out = p.stdout.strip()
                print("[cron %s] ran%s" % (
                    self.name, (": " + out[-200:]) if out else ""))
        except Exception as e:
            with self.lock:
                self.failures += 1
            sys.stderr.write("[cron %s] error: %s\n" % (self.name, e))


class Scheduler(threading.Thread):
    """Background thread: wakes every ~20s, fires due jobs."""

    def __init__(self, jobs):
        super().__init__(daemon=True, name="nova-cron")
        self.jobs = jobs
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            now = datetime.datetime.now()
            for job in self.jobs:
                if job.due(now):
                    t = threading.Thread(
                        target=job.fire, daemon=True,
                        name="cron-" + job.name)
                    t.start()
            self._stop.wait(20)


def jobs_from_manifest(man, man_path):
    """Build CronJobs from the manifest's `resource cron` entries."""
    appdir = os.path.dirname(os.path.abspath(man_path))
    jobs = []
    for kind, name, opts in man.resources:
        if kind != "cron":
            continue
        sched = opts.get("schedule")
        run_file = opts.get("run")
        if not sched or not run_file:
            raise ValueError(
                "resource cron '%s' needs schedule and run" % name)
        jobs.append(CronJob(name, sched, run_file, appdir))
    return jobs
