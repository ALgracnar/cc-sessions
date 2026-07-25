"""Remember which sessions were open, per window, across restarts.

The point is crash survival: every session is a child of the app process, so
if the app dies they all die with it. Their *conversations* survive in
~/.claude/projects, so this file records enough to offer them back.

Written CONTINUOUSLY, not on exit -- a segfault never gets to run cleanup code,
and that is exactly the case this exists for.
"""

import glob
import json
import os
import re
import time

STATE_DIR = os.path.expanduser("~/.config/cc-sessions")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def project_slug(cwd):
    """Claude's on-disk name for a project directory.

    EVERY character outside [A-Za-z0-9-] becomes a hyphen, not just the
    slashes: /home/x/nevron_dev/... becomes -home-x-nevron-dev-..., and
    ".claude" becomes "--claude". Getting this wrong meant transcripts were
    never found, so every restorable row claimed "transcript gone" and started
    a fresh session instead of resuming -- silently losing the conversation.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def transcript_path(cwd, session_id):
    """Where a session's transcript lives, by slug or by search."""
    if not session_id:
        return None
    direct = os.path.join(PROJECTS_DIR, project_slug(cwd), "%s.jsonl" % session_id)
    if os.path.exists(direct):
        return direct
    # A session id is unique, so if the slug rule ever changes again we can
    # still find the file by looking for it. Belt and braces, deliberately.
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", "%s.jsonl" % session_id))
    return matches[0] if matches else direct


def transcript_exists(cwd, session_id):
    path = transcript_path(cwd, session_id)
    return bool(path) and os.path.exists(path)


def load():
    """Return [[record, ...], ...] -- one list per window slot."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    windows = data.get("windows") if isinstance(data, dict) else None
    if not isinstance(windows, list):
        return []

    cleaned = []
    for window in windows:
        records = window.get("sessions") if isinstance(window, dict) else None
        if not isinstance(records, list):
            records = []
        cleaned.append([r for r in records if isinstance(r, dict) and r.get("cwd")])
    return cleaned


def save(windows):
    """windows: list of lists of record dicts."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        payload = {
            "saved_at": int(time.time()),
            "windows": [{"sessions": records} for records in windows],
        }
        # Write-then-rename so a crash mid-write cannot leave a truncated file
        # -- losing the session list to a botched save would defeat the point.
        temporary = STATE_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temporary, STATE_PATH)
    except OSError:
        pass


def record_for(sess):
    """Snapshot a live session in the shape we persist.

    Prefers what the roster says over what we launched with, because the two
    diverge: open a plain terminal tab, `cd` somewhere, run `claude` by hand,
    and the tab is now a Claude session in a different directory. Persisting
    the launch values would offer that back as a shell in the original folder
    -- which is exactly what it used to do.
    """
    session_id = getattr(sess, "session_id", None)
    return {
        "session_id": session_id,
        "name": sess.name,
        # Live cwd, so a restored terminal reopens where you left it
        # rather than where it was originally launched.
        "cwd": sess.current_cwd() if hasattr(sess, "current_cwd") else sess.cwd,
        # Having a session id means Claude is running in there, whatever this
        # tab was originally started as.
        "mode": "claude" if session_id else sess.mode,
        "status": getattr(sess, "last_status", None),
        "last_seen": int(time.time()),
    }
