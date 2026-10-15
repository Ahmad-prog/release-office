"""A pretend Jira, GitLab and set of agents, so the control page can be tried without touching anything."""

import threading
import time
from datetime import datetime, timezone

from office import plan
from office.config import ticket_dir
from office.facts import stamp
from office.state import add_report, now

TICKETS = {
    "DEMO-101": "Add the quarter and the admin run to the verdict report",
    "DEMO-102": "Read the shared CI token instead of the job's own",
    "DEMO-103": "List the measures added this month in the report",
}
BRIEF = """# Brief

- R1 to R4 as written on the ticket.
- D1: read the admin run from coredata.
- Files: the report module and its tests.
- Test plan: the report tests, then one saved bundle by hand.
"""
CONTEXT = """# Question for the owner

## What I was doing
Moving the job to the shared CI token, as the ticket asks.

## What I found
The old variable is still read by two other jobs in `.gitlab-ci.yml`. Removing it now would break them.

## The decision needed
Remove the old variable in this change, or keep it until those jobs have moved.

## Options
- Remove it now: one clean change, but the other two jobs must move in the same merge request, which the ticket puts out of scope.
- Keep it for now: this ticket stays small, and a follow-up ticket removes it.

## What I recommend
Keep it for now.
"""
MORE = "\n## More context, as the owner asked\nThe two jobs are `nightly-export` and `weekly-digest`. Neither is in this ticket's scope.\n"
QUESTION = (
    "The ticket says to use the shared token. Should the old variable be removed in this change, "
    "or kept until every job has moved over?"
)
PLAYS = {
    "working": ["Reading the ticket", "Writing the brief", "Editing the code", "Running the tests", "Tests pass"],
    "opening_mr": ["Pushing the branch", "Writing the description", "Opening the merge request"],
    "fixing": ["Reading the threads", "Fixing each thread", "Running the tests", "Pushing", "Replying on each thread"],
    "fixing_person": ["Reading the comment", "Making the change", "Running the tests", "Pushing", "Replying"],
    "co_review": ["Reading the local change", "Checking the tests", "Checking the docs"],
    "in_review": ["Reading the Jira ticket", "Mapping each requirement to the change", "Running the review"],
}


class FakeJira:
    def __init__(self):
        self.tickets = {key: {"summary": text, "status": "To Do", "moved": None} for key, text in TICKETS.items()}
        # One pretend ticket is written out of shape, to show the manager turning it away.
        start, middle, end = (plan.EXAMPLE.index(f"## {name}") for name in ("Requirements", "Decisions", "In scope"))
        self.tickets["DEMO-103"]["text"] = plan.EXAMPLE[:start] + plan.EXAMPLE[middle:end] + plan.EXAMPLE[start:middle] + plan.EXAMPLE[end:]
        self.comments = []

    def url(self, _key):
        return ""

    def move(self, key, status):
        self.tickets[key].update(status=status, moved=datetime.now(timezone.utc))

    def in_progress(self, _sprint, status):
        return [
            {"key": key, "summary": item["summary"], "type": "Story", "updated": item["moved"].isoformat()}
            for key, item in self.tickets.items()
            if item["status"] == status
        ]

    def entered_status_at(self, key, _status):
        return self.tickets[key]["moved"]

    def description_text(self, key):
        return self.tickets[key].get("text", plan.EXAMPLE)

    def summary(self, key):
        return self.tickets[key]["summary"]

    def status(self, key):
        return self.tickets[key]["status"]

    def transition(self, key, to_status):
        self.move(key, to_status)
        return True

    def comment(self, key, text):
        self.comments.append((key, text))

    def fix(self, key):
        self.tickets[key]["text"] = plan.EXAMPLE

    def waiting(self):
        return [{"key": key, "summary": item["summary"]} for key, item in self.tickets.items() if item["status"] == "To Do"]


class FakeGitLab:
    def __init__(self, owner):
        self.owner = owner
        self.mrs = {}
        self._next_mr = 2000
        self._next_note = 0

    def branch_exists(self, _name):
        return False

    def mr_for_branch(self, _branch):
        return None

    def open(self, branch, target):
        self._next_mr += 1
        self.mrs[self._next_mr] = {
            "state": "opened",
            "source_branch": branch,
            "target_branch": target,
            "approved": [],
            "threads": [],
            "reviewers": [],
            "assignees": [],
        }
        return self._next_mr

    def mr(self, iid):
        mr = self.mrs[iid]
        resolved = all(t["notes"][0]["resolved"] for t in mr["threads"] if t["notes"][0]["resolvable"])
        return {
            "state": mr["state"],
            "draft": False,
            "sha": "c0ffee1",
            "head_pipeline": {"status": "success", "sha": "c0ffee1"},
            "has_conflicts": False,
            "blocking_discussions_resolved": resolved,
            "source_branch": mr["source_branch"],
            "target_branch": mr["target_branch"],
            "web_url": f"https://git.example/demo/-/merge_requests/{iid}",
        }

    def approvals(self, iid):
        return list(self.mrs[iid]["approved"])

    def discussions(self, iid):
        return self.mrs[iid]["threads"]

    def _note(self, author, body, resolvable):
        self._next_note += 1
        return {
            "id": self._next_note,
            "system": False,
            "resolvable": resolvable,
            "resolved": False,
            "author": {"username": author},
            "body": body,
        }

    def add_thread(self, iid, author, body, resolvable=True):
        self.mrs[iid]["threads"].append({"id": f"t{self._next_note + 1}", "notes": [self._note(author, body, resolvable)]})

    def comment(self, iid, body):
        self.add_thread(iid, self.owner, body, resolvable=False)

    def set_reviewers(self, iid, usernames):
        self.mrs[iid]["reviewers"] = list(usernames)

    def set_assignees(self, iid, usernames):
        self.mrs[iid]["assignees"] = list(usernames)

    # ---- what the pretend agents and people do

    def reviewer_posts(self, iid, count):
        for index in range(count):
            self.add_thread(iid, self.owner, stamp("reviewer", f"**M{index + 1}.** A finding from the review."))

    def reviewer_resolves(self, iid):
        for thread in self.mrs[iid]["threads"]:
            if "release-office:reviewer" in thread["notes"][0]["body"]:
                thread["notes"][0]["resolved"] = True

    def developer_replies(self, iid):
        for thread in self.mrs[iid]["threads"]:
            first = thread["notes"][0]
            if first["resolvable"] and not first["resolved"] and "release-office" not in first["body"]:
                thread["notes"].append(self._note(self.owner, stamp("developer", "Fixed, and pushed."), True))

    def person_comments(self, iid, user):
        self.add_thread(iid, user, "Could this helper take the client as an argument instead of reading it globally?")

    def approve(self, iid, user):
        for thread in self.mrs[iid]["threads"]:
            if thread["notes"][0]["author"]["username"] == user:
                thread["notes"][0]["resolved"] = True
        if user not in self.mrs[iid]["approved"]:
            self.mrs[iid]["approved"].append(user)

    def merge(self, iid):
        self.mrs[iid]["state"] = "merged"


class DemoLauncher:
    """Plays each role with a short script instead of a real session."""

    kind = "demo"

    def __init__(self, cfg, store, gitlab, pace=1.1):
        self.cfg = cfg
        self.store = store
        self.gitlab = gitlab
        self.pace = pace
        self._thread = None

    def busy(self):
        return bool(self._thread and self._thread.is_alive())

    def launch(self, state, ticket, todo):
        role, key = todo["role"], ticket["key"]
        state["active"] = {"role": role, "ticket": key, "since": now(), "last": "Starting"}
        self._thread = threading.Thread(target=self._play, args=(key, role), daemon=True)
        self._thread.start()
        return True

    def _play(self, key, role):
        ticket = self.store.read()["tickets"][key]
        for line in PLAYS.get(ticket["state"], ["Working"]):
            time.sleep(self.pace)
            self.store.update(lambda state, said=line: _show(state, key, said))
        time.sleep(self.pace)
        status, extra = self._outcome(ticket, role)
        self.store.update(lambda state: _report(state, key, role, status, extra))

    def _outcome(self, ticket, role):
        key, current, iid = ticket["key"], ticket["state"], ticket.get("mr_iid")
        if role == "coauthor":
            if ticket["rounds"]["coauthor"] == 1:
                return "findings", {"count": 2, "file": f"coauthor-1.md for {key}"}
            return "clean", {}
        if role == "reviewer":
            if ticket["rounds"]["review"] == 1:
                self.gitlab.reviewer_posts(iid, 3)
                return "threads_posted", {"count": 3}
            self.gitlab.reviewer_resolves(iid)
            return "passed", {"note": "Every requirement on the ticket is met and my threads are resolved."}
        notes = ticket_dir(self.cfg, key)
        if current == "working" and ticket.get("clarifying"):
            with open(notes / "question.md", "a", encoding="utf-8") as handle:
                handle.write(MORE)
            return "blocked", {"question": QUESTION, "file": "question.md", "note": "I added the names of the two jobs to my note."}
        if current == "working" and key.endswith("102") and not ticket.get("demo_asked"):
            (notes / "brief.md").write_text(BRIEF, encoding="utf-8")
            (notes / "question.md").write_text(CONTEXT, encoding="utf-8")
            return "blocked", {"question": QUESTION, "file": "question.md", "demo_asked": True}
        if current == "working":
            return "built", {"note": "Tests pass."}
        if current == "opening_mr":
            return "mr_open", {"mr": self.gitlab.open(key, self.cfg["gitlab"]["target_branch"])}
        if current == "fixing_person":
            self.gitlab.developer_replies(iid)
        return "fixed", {"note": "Fixed and replied on each thread."}


def _show(state, key, text):
    if state.get("active") and state["active"]["ticket"] == key:
        state["active"]["last"] = text


def _report(state, key, role, status, extra):
    if state.get("active") and state["active"]["ticket"] == key:
        state["active"] = None
    if extra.pop("demo_asked", False):
        state["tickets"][key]["demo_asked"] = True
    add_report(state, key, role, status, **extra)
