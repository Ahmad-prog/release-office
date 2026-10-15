"""The task list: one JSON file, read and written under a file lock."""

import contextlib
import copy
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

EMPTY = {
    "version": 1,
    "mode": "manual",
    "running": False,
    "started_at": "",
    "tickets": {},
    "order": [],
    "reports": [],
    "events": [],
    "active": None,
    "seen": {},
    "seq": 0,
}
MAX_EVENTS = 300
MAX_REPORTS = 300


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """Every reader and writer goes through here, so the server and officectl never collide."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "office.json"

    @contextlib.contextmanager
    def _locked(self):
        with open(self.dir / ".lock", "w", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self):
        if not self.path.exists():
            return copy.deepcopy(EMPTY)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value in EMPTY.items():
            state.setdefault(key, copy.deepcopy(value))
        return state

    def _save(self, state):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def read(self):
        with self._locked():
            return self._load()

    def update(self, fn):
        """Load, let ``fn`` change the state in place, save. Returns what ``fn`` returns."""
        with self._locked():
            state = self._load()
            out = fn(state)
            self._save(state)
            return out


def next_id(state):
    state["seq"] += 1
    return state["seq"]


def add_event(state, text, ticket="", frm="manager", to="", attention=False):
    state["events"].append(
        {"id": next_id(state), "at": now(), "ticket": ticket, "from": frm, "to": to, "text": text, "attention": attention}
    )
    del state["events"][:-MAX_EVENTS]


def add_report(state, ticket, role, status, **extra):
    report = {"id": next_id(state), "at": now(), "ticket": ticket, "role": role, "status": status, "consumed": False}
    report.update({key: value for key, value in extra.items() if value not in (None, "")})
    state["reports"].append(report)
    del state["reports"][:-MAX_REPORTS]
    return report


def new_ticket(key, summary="", url=""):
    return {
        "key": key,
        "summary": summary,
        "url": url,
        "state": "listed",
        "waiting_on": "manager",
        "branch": key,
        "worktree": "",
        "mr_iid": None,
        "mr_url": "",
        "mr": {},
        "rounds": {"coauthor": 0, "review": 0},
        "todo": None,
        "running": "",
        "sessions": {},
        "question": None,
        "answer": "",
        "error": "",
        "seen_note_id": 0,
        "cost_usd": 0.0,
        "created_at": now(),
        "updated_at": now(),
    }
