#!/usr/bin/env python3
"""Stress test for cc-sessions.

Drives the real widgets hard: many sessions, rapid switching, reparenting
between two windows, zoom churn, and closes. Uses SHELL sessions, not Claude
ones -- they exercise the same VTE/stack/close code paths without burning API
calls, and the risky code (reparenting, PTY teardown, CSS reload) does not
care which program is running inside.

Run:  python3 stress_test.py [rounds]
Exit code 0 means it survived; a segfault or traceback means it did not.
"""

import os
import resource
import sys

os.environ.setdefault("GSK_RENDERER", "cairo")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")

from gi.repository import GLib, Gtk

_ROUNDS_ARG = int(sys.argv[1]) if len(sys.argv) > 1 else 60
sys.argv = [sys.argv[0]]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib.util
spec = importlib.util.spec_from_file_location("ccs", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cc-sessions.py"))
ccs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccs)

SESSIONS = 8
ROUNDS = _ROUNDS_ARG


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


class Stress:
    def __init__(self, app):
        self.app = app
        self.step = 0
        self.windows = []
        self.failures = []
        self.rss_start = None

    def log(self, message):
        print("  %s" % message, flush=True)

    def start(self):
        self.windows.append(self.app.new_window())
        self.log("phase 1: spawn %d shell sessions" % SESSIONS)
        for index in range(SESSIONS):
            self.app.start_session(
                name="stress-%d" % index,
                cwd=os.path.expanduser("~"),
                mode="shell",
            )
        self.windows[0].refresh()
        self.rss_start = rss_mb()
        GLib.timeout_add(120, self.tick)

    def tick(self):
        self.step += 1
        try:
            self.run_step(self.step)
        except Exception as exc:            # noqa: BLE001 - we want everything
            self.failures.append("step %d: %r" % (self.step, exc))
            self.log("FAILURE at step %d: %r" % (self.step, exc))

        if self.step >= ROUNDS:
            self.finish()
            return False
        return True

    def run_step(self, step):
        sessions = [s for s in self.app.sessions if not s.exited]
        if not sessions:
            return
        window = self.windows[step % len(self.windows)]

        # rapid switching, which exercises stack reparenting
        window.show_session(sessions[step % len(sessions)])

        if step == 10:
            self.log("phase 2: second window + move sessions across it")
            self.windows.append(self.app.new_window())

        if step % 7 == 0:
            # zoom churn: reloads CSS and re-applies fonts to every terminal
            self.app.set_terminal_zoom(1.0 + ((step // 7) % 5) * 0.15, persist=False)
            self.app.set_sidebar_zoom(1.0 + ((step // 7) % 3) * 0.1, persist=False)

        if step % 11 == 0:
            for win in self.windows:
                win._last_signature = None
                win.refresh()

        if step == 35:
            self.log("phase 3: close half the sessions")
            for sess in list(self.app.sessions)[: SESSIONS // 2]:
                self.app.close_session(sess)

        if step == 45:
            self.log("phase 4: spawn replacements while others run")
            for index in range(3):
                self.app.start_session(
                    name="stress-late-%d" % index,
                    cwd=os.path.expanduser("~"),
                    mode="shell",
                )

    def finish(self):
        alive = [s for s in self.app.sessions if not s.exited]
        self.log("")
        self.log("closing everything down")
        for sess in list(self.app.sessions):
            self.app.close_session(sess)

        growth = rss_mb() - self.rss_start
        self.log("")
        self.log("steps run        : %d" % self.step)
        self.log("sessions at end  : %d" % len(alive))
        self.log("memory growth    : %+.1f MB" % growth)
        self.log("failures         : %d" % len(self.failures))
        for failure in self.failures:
            self.log("   - %s" % failure)
        self.log("")
        self.log("RESULT: %s" % ("PASS" if not self.failures else "FAIL"))
        self.app.quit()


def main():
    app = ccs.CCSessions(non_unique=True)
    stress = Stress(app)

    def on_activate(_app):
        stress.start()

    app.connect("activate", on_activate)
    app.run([])
    return 1 if stress.failures else 0


if __name__ == "__main__":
    sys.exit(main())
