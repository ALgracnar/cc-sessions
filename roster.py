"""Read Claude Code's live session roster.

Every running Claude Code session writes ~/.claude/sessions/<pid>.json. The
interesting field is `status`, one of:

    busy    -- actively working (or a subagent is running)
    shell   -- a shell command has the terminal
    waiting -- BLOCKED ON YOU; `waitingFor` says what it wants
    idle    -- finished, nothing pending

That last distinction is the whole point of this app: a terminal tab title can
only say "working" or "not working", but `waiting` means get up and go look.

Only CLI sessions publish a status; desktop-app sessions write the file without
one. We tolerate any value, including unknown ones, in case the enum grows.
"""

import glob
import json
import os
import time

ROSTER_DIR = os.path.expanduser("~/.claude/sessions")

# Sort order: things that want you, first.
STATUS_RANK = {"waiting": 0, "busy": 1, "shell": 2, "idle": 3}
UNKNOWN_RANK = 2.5


def _proc_start_ticks(pid):
    """Kernel start time of a pid, used to detect pid reuse."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        return None
    # comm may contain spaces/parens, so parse from the last ')'.
    close = data.rfind(b")")
    if close == -1:
        return None
    fields = data[close + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return fields[19].decode()
    except (IndexError, UnicodeDecodeError):
        return None


class Entry:
    """One live session, as described by its roster file."""

    def __init__(self, data, path):
        self.path = path
        self.pid = data.get("pid")
        self.session_id = data.get("sessionId")
        self.name = data.get("name") or "(unnamed)"
        self.name_source = data.get("nameSource")
        self.cwd = data.get("cwd") or ""
        self.status = data.get("status")
        self.waiting_for = data.get("waitingFor")
        self.entrypoint = data.get("entrypoint") or "?"
        self.version = data.get("version")
        self.status_updated_at = data.get("statusUpdatedAt")
        self.started_at = data.get("startedAt")
        self.proc_start = data.get("procStart")
        self.alive = self._check_alive()

    def _check_alive(self):
        if not isinstance(self.pid, int):
            return False
        actual = _proc_start_ticks(self.pid)
        if actual is None:
            return False
        # If the roster recorded a start time, insist it matches -- otherwise a
        # recycled pid would make a dead session look alive.
        if self.proc_start and str(self.proc_start) != actual:
            return False
        return True

    @property
    def rank(self):
        return STATUS_RANK.get(self.status, UNKNOWN_RANK)

    @property
    def status_age(self):
        """Seconds spent in the current status, or None if unknown."""
        if not self.status_updated_at:
            return None
        return max(0.0, time.time() - self.status_updated_at / 1000.0)

    @property
    def short_cwd(self):
        home = os.path.expanduser("~")
        cwd = self.cwd
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        # A worktree path is long and its tail is the informative part.
        parts = cwd.split(os.sep)
        if len(parts) > 3:
            return os.sep.join(["…"] + parts[-2:])
        return cwd

    @property
    def status_label(self):
        if self.status is None:
            return "—"
        return self.status

    def describe_age(self):
        age = self.status_age
        if age is None:
            return ""
        if age < 60:
            return "%ds" % int(age)
        if age < 3600:
            return "%dm" % int(age // 60)
        return "%dh%02dm" % (int(age // 3600), int((age % 3600) // 60))


def read(include_dead=False):
    """Return the current roster, most-urgent first."""
    entries = []
    for path in glob.glob(os.path.join(ROSTER_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "pid" not in data:
            continue
        entry = Entry(data, path)
        if entry.alive or include_dead:
            entries.append(entry)

    entries.sort(key=lambda e: (e.rank, -(e.status_age or 0), e.name.lower()))
    return entries


def signature(entries):
    """A cheap value that changes whenever the visible state changes.

    Used to avoid rebuilding the sidebar on every tick when nothing moved.
    """
    return tuple(
        (e.pid, e.name, e.status, e.waiting_for, int((e.status_age or 0) // 1))
        for e in entries
    )
