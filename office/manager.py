"""The manager: a state machine that carries each ticket from In Progress to staging.

It decides and relays. It never writes or reviews code. The developer and the reviewer
never speak to each other: each reports here, and the next message goes out from here.
"""

import copy
from datetime import datetime, timezone
from pathlib import Path

from office import plan
from office.api import ApiError
from office.config import HUMAN_REVIEWER, shown, state_dir, ticket_dir
from office.facts import mr_facts, stamp
from office.gitops import GitError, add_worktree, fetch
from office.state import add_event, new_ticket, now

# States in which an agent holds the ticket.
IN_FLIGHT = {"listed", "working", "co_review", "opening_mr", "in_review", "fixing", "fixing_person"}
WITH_PEOPLE = {"human_review", "waiting_merge"}

LANE = {
    "queued": "developer",
    "listed": "developer",
    "working": "developer",
    "co_review": "developer",
    "opening_mr": "developer",
    "fixing": "developer",
    "fixing_person": "developer",
    "in_review": "reviewer",
    "human_review": "human_reviewer",
    "waiting_merge": "merger",
    "on_staging": "staging",
    "blocked": "owner",
    "needs_plan": "owner",
    "needs_owner": "owner",
}
LABEL = {
    "queued": "Waiting its turn",
    "listed": "On the list",
    "working": "Building",
    "co_review": "Co-author reading",
    "opening_mr": "Opening the merge request",
    "in_review": "Agent review",
    "fixing": "Fixing review threads",
    "fixing_person": "Answering a comment",
    "human_review": "Waiting for review",
    "waiting_merge": "Waiting for merge",
    "on_staging": "Deployed to staging",
    "blocked": "Question for you",
    "needs_plan": "Ticket needs fixing",
    "needs_owner": "Needs your decision",
}

MSG = {
    "start": (
        "New ticket {key}: {summary}\n"
        "Read it with `officectl ticket {key}`, write the brief, then build and test on branch {key}. "
        "Decide small things that do not change the scope yourself and note each one under \"Decisions I made\". "
        "Report `built` when the change is ready for the co-author, or `blocked` only with a question the owner must settle."
    ),
    "coauthor": (
        "Read the local change for {key} (branch {key} against origin/{target}) before any merge request exists. "
        "Write your findings to {file}. Report `clean` or `findings`."
    ),
    "findings": (
        "The co-author found {count} item(s), written in {file}. "
        "Fix what is right, re-run the tests, then report `built`."
    ),
    "findings_last": (
        "The co-author found {count} item(s), written in {file}. This was the last co-author round: "
        "fix what is right, then push, open the merge request and report `mr_open`."
    ),
    "open_mr": (
        "The co-author found nothing to fix. Push the branch, open the merge request with "
        "`officectl mr-create`, and report `mr_open`."
    ),
    "review": (
        "Review merge request !{iid} for {key}. Make sure it maps exactly to the Jira ticket: every "
        "requirement and every decision must match. Post each mismatch or finding as a thread. "
        "Report `threads_posted` or `passed`."
    ),
    "threads": (
        "The reviewer posted {count} thread(s) on !{iid}. Fix each one, push, and reply on each thread "
        "with what changed. Do not resolve any thread. Report `fixed`."
    ),
    "recheck": (
        "The developer pushed fixes on !{iid}. Check every fix against the code, resolve the threads you "
        "have verified, and post anything new. Report `threads_posted` or `passed`."
    ),
    "person": (
        "There are new comments on !{iid} from {who}. Address each one, push, and reply where the comment was made. "
        "Do not resolve a person's thread. Report `fixed`."
    ),
    "pipeline": "The pipeline failed on !{iid}. Find the failing job, fix it, push, and report `fixed`.",
    "conflict": (
        "!{iid} has conflicts with {target}. Rebase the branch onto origin/{target}, re-run the tests, push "
        "with `officectl push --ticket {key} --force-with-lease`, and report `fixed`."
    ),
    "answer": (
        "The owner answered your question on {key}.\nQuestion: {question}\nAnswer: {answer}\n"
        "Carry on from where you stopped."
    ),
    "clarify": (
        "The owner needs more context before he can answer your question on {key}.\n"
        "Your question: {question}\nHe asks: {followup}\n"
        "Add what he asks for to your question file, then report `blocked` again with the same question and --file."
    ),
    "resume": "\n\nNote from the owner, who has looked at this ticket: {note}",
    "as_it_is": (
        "\n\nNote from the owner: this ticket is started as it is. It is not written in the office's set shape, "
        "and `officectl ticket-check` will say so. Do not report `blocked` for its shape. Work from the "
        "ticket's own requirements, acceptance criteria and decisions, and report `blocked` only for a real question."
    ),
    "ask_human": (
        "This merge request for {key} has passed the Release Office's own review: the reviewer agent's "
        "threads are all resolved. It is now with the human reviewer, whose approval sends it on for merge."
    ),
    "ask_human_direct": (
        "This merge request for {key} is ready for review. The developer agent built it, and the co-author "
        "agent checked it against the ticket's requirements and scope. It is with the human reviewer, whose "
        "approval sends it on for merge."
    ),
    "ask_merge": (
        "@{user} this merge request for {key} is approved by {approver}, its pipeline passed and every "
        "thread is resolved. It is assigned to you for merge."
    ),
    # The only two things the office writes on a Jira ticket.
    "decision": (
        "Core design decision, recorded by the Release Office manager.\n\nQuestion from the developer agent: {question}\n\n"
        "Decision by {owner}: {answer}"
    ),
    "mr_link": "Merge request: {url}",
}


REVIEWER_AGENT = "the reviewer agent"


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _parse(stamp_utc):
    if not stamp_utc:
        return None
    return datetime.strptime(stamp_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Manager:
    def __init__(self, cfg, store, jira=None, gitlab=None, launcher=None, token=""):
        self.cfg = cfg
        self.store = store
        self.jira = jira
        self.gitlab = gitlab
        self.launcher = launcher
        self.token = token
        self.dry = False
        self.log = []

    # ------------------------------------------------------------------ public

    def tick(self, dry=False):
        """One pass over the task list. With ``dry`` nothing outside this process is changed."""
        self.dry, self.log = dry, []
        if dry:
            self._tick(copy.deepcopy(self.store.read()))
        else:
            self.store.update(self._tick)
        return list(self.log)

    def start(self):
        def go(state):
            state["running"] = True
            state["started_at"] = state["started_at"] or now()
            add_event(state, "The office is open.", to="owner")

        self.store.update(go)

    def stop(self):
        def halt(state):
            state["running"] = False
            add_event(state, "The office is closed. Nothing new will start.", to="owner")

        self.store.update(halt)

    def add(self, key, adopt=False, unplanned=False):
        """Put a ticket on the list by hand."""
        summary = self.jira.summary(key) if self.jira else ""

        def put(state):
            if key in state["tickets"]:
                return False
            self._add(state, key, summary, by_hand=True)
            state["tickets"][key]["adopt"] = adopt
            state["tickets"][key]["unplanned_ok"] = bool(unplanned)
            return True

        return self.store.update(put)

    def answer(self, key, text, record=False):
        """The owner's answer to a blocked ticket. The next tick passes it on.

        It goes on the Jira ticket only with ``record``: when it changes a core design decision.
        """

        def put(state):
            ticket = state["tickets"].get(key)
            if not ticket or ticket["state"] != "blocked":
                return False
            ticket["answer"] = text.strip()
            ticket["record_answer"] = bool(record)
            add_event(state, text.strip(), key, frm="owner", to="manager")
            return True

        return self.store.update(put)

    def follow_up(self, key, text):
        """The owner wants more context before he answers. The next tick asks the developer for it."""

        def put(state):
            ticket = state["tickets"].get(key)
            if not ticket or ticket["state"] != "blocked" or not text.strip():
                return False
            ticket["followup"] = text.strip()
            ticket.setdefault("exchange", []).append({"from": "owner", "text": text.strip(), "at": now()})
            add_event(state, text.strip(), key, frm="owner", to="manager")
            return True

        return self.store.update(put)

    def resume(self, key, to_state="", note="", unplanned=False):
        """After the owner has sorted out a stuck ticket, put it back where it stopped."""

        def put(state):
            ticket = state["tickets"].get(key)
            target = to_state or (ticket or {}).get("stuck_from") or "working"
            stuck = ("needs_owner", "needs_plan")
            if not ticket or ticket["state"] not in stuck or target not in LANE or target in stuck:
                return False
            ticket["error"] = ""
            ticket.pop("approved_sha", None)
            ticket["unplanned_ok"] = bool(unplanned or ticket.get("unplanned_ok"))
            waiting = {"human_reviewer": self.cfg["people"]["human_reviewer"], "merger": self.cfg["people"]["merger"]}
            self._set(state, ticket, target, waiting.get(LANE[target], LANE[target]))
            add_event(state, f"Put back in the flow as: {LABEL[target].lower()}. {note}".strip(), key, frm="owner")
            last = ticket.get("last_todo")
            if last and target in IN_FLIGHT:
                # Whoever held the ticket gets the same instruction again, with the owner's note.
                again = last["message"] + (MSG["resume"].format(note=note) if note else "")
                self._assign(state, ticket, last["role"], again)
            return True

        return self.store.update(put)

    # ------------------------------------------------------------------ one pass

    def _tick(self, state):
        if state["running"]:
            self._guard(state, None, lambda: self._pickup(state))
        for key in list(state["order"]):
            ticket = state["tickets"][key]
            self._guard(state, ticket, lambda t=ticket: self._step(state, t))
        if state["running"]:
            self._dispatch(state)

    def _guard(self, state, ticket, fn):
        """Jira, GitLab or git being unreachable must not stop the loop. Say so once, and carry on."""
        try:
            fn()
        except (ApiError, GitError, OSError) as exc:
            text = f"Could not finish a step: {exc}"
            self.log.append(text)
            if state.get("last_error") != text:
                state["last_error"] = text
                add_event(state, text, ticket["key"] if ticket else "", to="owner", attention=True)

    def _pickup(self, state):
        """Take the owner's In Progress tickets, as far as there is room."""
        policy = self.cfg["jira"]["pickup"]
        limit = self.cfg["limits"]["in_flight"]
        if not self.jira or policy == "manual_only" or self._in_flight(state, waiting=True) >= limit:
            return
        status = self.cfg["jira"]["status"]["in_progress"]
        started = _parse(state["started_at"])
        for item in self.jira.in_progress(self.cfg["jira"]["sprint"], status):
            key = item["key"]
            if key in state["tickets"] or state["seen"].get(key) == item["updated"]:
                continue
            if policy == "after_start":
                moved = self.jira.entered_status_at(key, status)
                if not moved or not started or moved < started:
                    state["seen"][key] = item["updated"]
                    continue
            self._add(state, key, item["summary"])
            if self._in_flight(state, waiting=True) >= limit:
                return

    def _add(self, state, key, summary, by_hand=False):
        url = self.jira.url(key) if self.jira else ""
        state["tickets"][key] = new_ticket(key, summary, url)
        state["order"].append(key)
        how = "added it by hand" if by_hand else "moved it to In Progress"
        text = f"{self.cfg['owner']['name']} {how}. It is on the list."
        add_event(state, text, key, frm="owner", to="manager")
        self.log.append(f"{key}: {text}")

    def _in_flight(self, state, waiting=False):
        """How many tickets the agents hold. With ``waiting``, a fixed ticket in the queue counts too."""
        busy = set(IN_FLIGHT) | ({"queued"} if waiting else set())
        if self.cfg["start_next_when"] == "waiting_merge":
            busy.add("human_review")
        return sum(1 for ticket in state["tickets"].values() if ticket["state"] in busy)

    def _step(self, state, ticket):
        current = ticket["state"]
        if current in ("on_staging", "needs_owner"):
            return None
        if current == "blocked":
            return self._blocked(state, ticket)
        if current == "needs_plan":
            return self._replan(state, ticket)
        if current == "queued":
            return self._start_if_room(state, ticket)
        if current in WITH_PEOPLE:
            return self._with_people(state, ticket)
        if current == "listed":
            # Nothing is started for a ticket while the office is closed.
            return self._listed(state, ticket) if state["running"] else None
        if current == "in_review" and not self.cfg["agent_review"]["enabled"] and not ticket["running"]:
            # The reviewer's desk is closed. A ticket left there goes to the human reviewer.
            ticket["todo"] = None
            return self._to_human(state, ticket, after_agent=False)
        report = self._take(state, ticket)
        if not report:
            return None
        before, old_todo = state["seq"], ticket.get("todo")
        self._accept(state, ticket, report)
        try:
            return self._route(state, ticket, report)
        except (ApiError, GitError, OSError):
            # Nothing was decided, so leave the report for the next pass.
            report["consumed"] = False
            ticket["todo"] = old_todo
            state["events"] = [event for event in state["events"] if event["id"] <= before]
            raise

    def _route(self, state, ticket, report):
        current = ticket["state"]
        role, status = report["role"], report["status"]
        if status == "failed":
            why = report.get("note") or "no reason given"
            return self._needs_owner(state, ticket, f"The {role} could not finish: {why}")
        if role == "developer" and status == "blocked":
            return self._block(state, ticket, report)
        handlers = {
            ("working", "developer", "built"): self._built,
            ("working", "developer", "mr_open"): self._mr_open,
            ("opening_mr", "developer", "mr_open"): self._mr_open,
            ("co_review", "coauthor", "clean"): self._co_clean,
            ("co_review", "coauthor", "findings"): self._co_findings,
            ("in_review", "reviewer", "threads_posted"): self._threads,
            ("in_review", "reviewer", "passed"): self._passed,
            ("fixing", "developer", "fixed"): self._fixed,
            ("fixing_person", "developer", "fixed"): self._person_fixed,
        }
        handler = handlers.get((current, role, status))
        if handler:
            return handler(state, ticket, report)
        text = f"Set aside an unexpected report: the {role} said `{status}` while the ticket was {LABEL[current].lower()}."
        return self._note(state, ticket, text, attention=True)

    def _dispatch(self, state):
        """Hand the oldest waiting message to its role. One agent session runs at a time."""
        if self.dry or not self.launcher or self.launcher.busy():
            return
        for key in state["order"]:
            ticket = state["tickets"][key]
            todo = ticket.get("todo")
            if not todo or ticket["running"] or todo.get("handed"):
                continue
            if self.launcher.launch(state, ticket, todo):
                ticket["running"] = todo["role"]
                return
            todo["handed"] = True  # manual mode: the owner runs the skill himself

    # ------------------------------------------------------------------ small tools

    def _do(self, text, fn, critical=False):
        """Record an outside action and, unless this is a preview, perform it."""
        self.log.append(text)
        if self.dry or fn is None:
            return True
        try:
            fn()
            return True
        except (ApiError, GitError, OSError) as exc:
            if critical:
                raise
            self.log.append(f"FAILED: {text} ({exc})")
            return False

    def _set(self, state, ticket, to_state, waiting_on):
        ticket["state"] = to_state
        ticket["waiting_on"] = waiting_on or ""
        ticket["updated_at"] = now()

    def _note(self, state, ticket, text, to="owner", attention=False):
        add_event(state, text, ticket["key"], to=to, attention=attention)
        self.log.append(f"{ticket['key']}: {text}")

    def _assign(self, state, ticket, role, message, fresh=False):
        ticket["todo"] = {"role": role, "message": message, "fresh": fresh, "at": now()}
        ticket["last_todo"] = {"role": role, "message": message}
        add_event(state, message, ticket["key"], to=role)
        self.log.append(f"{ticket['key']}: tell the {role}: {message.splitlines()[0]}")

    def _take(self, state, ticket):
        """The oldest report for this ticket that has not been acted on."""
        for report in state["reports"]:
            if report["ticket"] == ticket["key"] and not report["consumed"]:
                return report
        return None

    def _accept(self, state, ticket, report):
        report["consumed"] = True
        if ticket.get("todo") and ticket["todo"]["role"] == report["role"]:
            ticket["todo"] = None
        ticket["running"] = ""
        said = f"Reported `{report['status']}`. {report.get('note') or report.get('question') or ''}".strip()
        add_event(state, said, ticket["key"], frm=report["role"], to="manager")

    def _name(self, username):
        return shown(self.cfg, username)

    def _move(self, state, ticket, which):
        """Move the Jira ticket. A refusal is flagged to the owner and does not stop the flow."""
        target = self.cfg["jira"]["status"][which]
        if not self.jira:
            return
        key = ticket["key"]

        def go():
            if not self.jira.transition(key, target):
                raise ApiError(409, f"Jira offers no move to {target} from where {key} is now")

        if not self._do(f"Move {key} to {target} in Jira", go):
            self._note(state, ticket, f"Could not move {key} to {target} in Jira. Please move it by hand.", attention=True)

    def _needs_owner(self, state, ticket, why):
        ticket["error"] = why
        ticket["stuck_from"] = ticket["state"]
        ticket["todo"] = None
        self._note(state, ticket, why, attention=True)
        self._set(state, ticket, "needs_owner", "owner")

    def _back_to_developer(self, state, ticket, message):
        ticket["return_to"] = ticket["state"]
        self._assign(state, ticket, "developer", message)
        self._set(state, ticket, "fixing_person", "developer")

    # ------------------------------------------------------------------ steps

    def _plan_problems(self, ticket):
        """What is wrong with the ticket's description. Empty when the office can run it."""
        if not self.jira or not self.cfg["jira"].get("require_plan") or ticket.get("unplanned_ok"):
            return []
        return plan.check(self.jira.description_text(ticket["key"]))

    def _reject(self, state, ticket, problems):
        """The ticket is not written the way the office needs. Tell the owner on the page, once, and wait."""
        if ticket.get("plan_problems") != problems:
            self._note(state, ticket, f"Not started. {' '.join(problems)}", attention=True)
        ticket["plan_problems"] = problems
        ticket["error"] = "Fix the ticket's description. The office starts it as soon as it passes the check."
        ticket["stuck_from"] = "listed"
        ticket["todo"] = None
        return self._set(state, ticket, "needs_plan", "owner")

    def _replan(self, state, ticket):
        """The owner was asked to fix the ticket. Look again, and start it once it passes."""
        problems = self._plan_problems(ticket)
        if problems:
            return self._reject(state, ticket, problems)
        ticket["plan_problems"], ticket["error"] = [], ""
        self._note(state, ticket, "The ticket now passes the check.")
        self._set(state, ticket, "queued", "manager")
        return self._start_if_room(state, ticket)

    def _start_if_room(self, state, ticket):
        """A fixed ticket starts as soon as the agents are free. It does not jump the limit."""
        if self._in_flight(state) >= self.cfg["limits"]["in_flight"]:
            return None
        self._set(state, ticket, "listed", "manager")
        return self._listed(state, ticket, checked=True)

    def _listed(self, state, ticket, checked=False):
        key = ticket["key"]
        target = self.cfg["gitlab"]["target_branch"]
        # The ticket's shape is checked before anything else is done with it.
        problems = [] if checked else self._plan_problems(ticket)
        if problems:
            return self._reject(state, ticket, problems)
        if self.gitlab and not ticket.get("adopt"):
            # A closed or merged request from an earlier try is no obstacle. An open one is.
            earlier = self.gitlab.mr_for_branch(key) or {}
            if self.gitlab.branch_exists(key) or earlier.get("state") == "opened":
                why = f"A branch or an open merge request named {key} already exists, and the office did not create it."
                return self._needs_owner(state, ticket, why)
        if getattr(self.launcher, "kind", "") == "auto":
            text = f"Create a working copy for {key} from origin/{target}"
            self._do(text, lambda: self._prepare(ticket), critical=True)
        start = MSG["start"].format(key=key, summary=ticket["summary"]) + (MSG["as_it_is"] if ticket.get("unplanned_ok") else "")
        self._assign(state, ticket, "developer", start, fresh=True)
        return self._set(state, ticket, "working", "developer")

    def _prepare(self, ticket):
        repo = self.cfg["repo"]["path"]
        path = Path(self.cfg["repo"]["worktrees"]).expanduser() / ticket["key"]
        if not path.exists():
            target = self.cfg["gitlab"]["target_branch"]
            fetch(repo, target, self.token, str(state_dir(self.cfg)))
            add_worktree(repo, path, ticket["key"], f"origin/{target}")
        ticket["worktree"] = str(path)

    def _block(self, state, ticket, report):
        question = report.get("question") or report.get("note") or "The developer is blocked and gave no question."
        # ``design``: the developer says the answer would change a core design decision.
        ticket["question"] = {"text": question, "at": now(), "file": report.get("file", ""), "design": bool(report.get("design"))}
        # After a follow-up, what the developer adds is the news, not the question again.
        said = report.get("note") or "I have added that to my note." if ticket.get("clarifying") else question
        ticket.setdefault("exchange", []).append({"from": "developer", "text": said, "at": now()})
        ticket["clarifying"] = False
        ticket["prev_state"] = ticket["state"]
        self._note(state, ticket, f"The developer needs your answer: {question}", attention=True)
        return self._set(state, ticket, "blocked", "owner")

    def _blocked(self, state, ticket):
        answer = ticket.get("answer")
        key = ticket["key"]
        question = (ticket.get("question") or {}).get("text", "")
        if not answer and ticket.get("followup"):
            # The owner asked for more first. That goes to the developer, and the question stays open.
            followup, ticket["followup"], ticket["clarifying"] = ticket["followup"], "", True
            self._assign(state, ticket, "developer", MSG["clarify"].format(key=key, question=question, followup=followup))
            return self._set(state, ticket, ticket.get("prev_state", "working"), "developer")
        if not answer:
            return None
        # Every answer is kept in the ticket's notes, where the developer and the reviewer read it.
        kept = ticket_dir(self.cfg, key) / "decisions.md"
        entry = f"## {now()}\n\nQuestion from the developer: {question}\n\nAnswer from the owner: {answer}\n\n"
        self._do(f"Keep the answer in {kept.name}", lambda: kept.write_text(_read(kept) + entry, encoding="utf-8"))
        # Jira hears of it only when the owner says it changes a core design decision.
        if self.jira and ticket.pop("record_answer", False):
            text = MSG["decision"].format(question=question, owner=self.cfg["owner"]["name"], answer=answer)
            self._do(f"Write the core design decision on {key} in Jira", lambda: self.jira.comment(key, text))
        ticket["answer"], ticket["question"], ticket["exchange"], ticket["followup"] = "", None, [], ""
        self._assign(state, ticket, "developer", MSG["answer"].format(key=key, question=question, answer=answer))
        return self._set(state, ticket, ticket.pop("prev_state", "working"), "developer")

    def _built(self, state, ticket, _report):
        ticket["rounds"]["coauthor"] += 1
        findings = ticket_dir(self.cfg, ticket["key"]) / f"coauthor-{ticket['rounds']['coauthor']}.md"
        message = MSG["coauthor"].format(
            key=ticket["key"], target=self.cfg["gitlab"]["target_branch"], file=findings
        )
        self._assign(state, ticket, "coauthor", message, fresh=True)
        return self._set(state, ticket, "co_review", "coauthor")

    def _co_clean(self, state, ticket, _report):
        self._assign(state, ticket, "developer", MSG["open_mr"])
        return self._set(state, ticket, "opening_mr", "developer")

    def _co_findings(self, state, ticket, report):
        last = ticket["rounds"]["coauthor"] >= self.cfg["limits"]["coauthor_loops"]
        if last and report.get("drift"):
            # The co-author is the check on scope. A change that still drifts does not go out for review.
            where = report.get("file", "the findings file")
            why = f"After {ticket['rounds']['coauthor']} co-author rounds the change still does not match the ticket's scope. See {where}."
            return self._needs_owner(state, ticket, why)
        message = MSG["findings_last" if last else "findings"].format(
            count=report.get("count", "some"), file=report.get("file", "the findings file")
        )
        self._assign(state, ticket, "developer", message)
        return self._set(state, ticket, "opening_mr" if last else "working", "developer")

    def _mr_open(self, state, ticket, report):
        iid = report.get("mr") or ticket.get("mr_iid")
        if not iid:
            return self._needs_owner(state, ticket, "The developer reported a merge request without its number.")
        iid = int(iid)
        target = self.cfg["gitlab"]["target_branch"]
        if self.gitlab:
            mr = self.gitlab.mr(iid)
            if mr.get("source_branch") != ticket["branch"] or mr.get("target_branch") != target:
                why = f"!{iid} is not {ticket['branch']} into {target}, so the office will not take it further."
                return self._needs_owner(state, ticket, why)
            ticket["mr_url"] = mr.get("web_url", "")
        ticket["mr_iid"] = iid
        self._move(state, ticket, "completed")
        if self.jira and ticket["mr_url"]:
            # The one thing the office always writes on the ticket: where the merge request is.
            link = MSG["mr_link"].format(url=ticket["mr_url"])
            self._do(f"Write the merge request link on {ticket['key']} in Jira", lambda: self.jira.comment(ticket["key"], link))
        if not self.cfg["agent_review"]["enabled"]:
            # No reviewer agent for now: the developer and the co-author are done, so a person reviews.
            return self._to_human(state, ticket, after_agent=False)
        ticket["rounds"]["review"] = 1
        self._assign(state, ticket, "reviewer", MSG["review"].format(iid=iid, key=ticket["key"]), fresh=True)
        return self._set(state, ticket, "in_review", "reviewer")

    def _threads(self, state, ticket, report):
        message = MSG["threads"].format(count=report.get("count", "some"), iid=ticket["mr_iid"])
        self._assign(state, ticket, "developer", message)
        return self._set(state, ticket, "fixing", "developer")

    def _fixed(self, state, ticket, _report):
        if not self.cfg["agent_review"]["enabled"]:
            return self._to_human(state, ticket, after_agent=False)
        rounds = self.cfg["limits"]["review_rounds"]
        if ticket["rounds"]["review"] >= rounds:
            why = f"{rounds} review rounds have not cleared the threads on !{ticket['mr_iid']}."
            return self._needs_owner(state, ticket, why)
        ticket["rounds"]["review"] += 1
        self._assign(state, ticket, "reviewer", MSG["recheck"].format(iid=ticket["mr_iid"]))
        return self._set(state, ticket, "in_review", "reviewer")

    def _passed(self, state, ticket, _report):
        """The reviewer agent has closed its threads. A person reviews next."""
        return self._to_human(state, ticket, after_agent=True)

    def _to_human(self, state, ticket, after_agent):
        """Give the merge request to the human reviewer, and from here on watch it."""
        iid, key = ticket["mr_iid"], ticket["key"]
        people = self.cfg["people"]
        said = f"The reviewer agent passed !{iid}." if after_agent else f"!{iid} is ready: the developer and the co-author are done."
        if not self.cfg["human_review"]["enabled"]:
            # No person reviews: once the pipeline is green and every thread resolved, it goes for merge.
            self._note(state, ticket, f"{said} It goes to {self._name(self.cfg['people']['merger'])} once its checks pass.", to="owner")
            return self._set(state, ticket, "human_review", "checks")
        user = people["human_reviewer"]
        if not user:
            why = (
                f"{said} But no human reviewer is set. Put the GitLab username under "
                "people.human_reviewer in office.config.json, then put the ticket back in the flow."
            )
            return self._needs_owner(state, ticket, why)
        if self.gitlab:
            self._do(f"Set {HUMAN_REVIEWER} as reviewer on !{iid}", lambda: self.gitlab.set_reviewers(iid, [user]))
            ask = stamp("manager", MSG["ask_human" if after_agent else "ask_human_direct"].format(key=key))
            self._do(f"Ask {HUMAN_REVIEWER} for the review on !{iid}", lambda: self.gitlab.comment(iid, ask))
            left = 0 if after_agent else mr_facts(self.gitlab, iid)["agent_open"]
            if left:
                # Nobody is there to close these now, and GitLab will not merge with a thread open.
                text = f"{left} thread(s) the reviewer agent opened on !{iid} are still open. Resolve them on GitLab yourself."
                self._note(state, ticket, text, attention=True)
        add_event(state, f"{said} Please review it.", key, to=user)
        return self._set(state, ticket, "human_review", user)

    def _person_fixed(self, state, ticket, _report):
        back = ticket.pop("return_to", "human_review")
        people = self.cfg["people"]
        if back == "waiting_merge":
            text = f"The developer pushed changes to !{ticket['mr_iid']} after it was approved. Ask for another look if it needs one."
            self._note(state, ticket, text, attention=True)
        return self._set(state, ticket, back, people["merger"] if back == "waiting_merge" else people["human_reviewer"])

    def _with_people(self, state, ticket):
        """The merge request is with a person. Watch it, and pass on whatever they say."""
        if not self.gitlab or not ticket.get("mr_iid"):
            return None
        iid, key = ticket["mr_iid"], ticket["key"]
        target = self.cfg["gitlab"]["target_branch"]
        facts = mr_facts(self.gitlab, iid, ticket.get("seen_note_id", 0))
        ticket["mr"] = {
            name: facts[name]
            for name in ("state", "pipeline", "approved_by", "agent_open", "people_open", "draft", "conflicts")
        }
        if facts["state"] == "merged":
            return self._merged(state, ticket)
        if facts["state"] == "closed":
            return self._needs_owner(state, ticket, f"!{iid} was closed without being merged.")
        spoke = facts["person_threads"] + facts["new_comments"]
        if spoke:
            ticket["seen_note_id"] = facts["last_note_id"]
            who = " and ".join(sorted({self._name(item["last_author"]) for item in spoke}))
            return self._back_to_developer(state, ticket, MSG["person"].format(who=who, iid=iid))
        if ticket["state"] == "waiting_merge":
            return None

        owner = self.cfg["owner"]["gitlab_username"]
        others = [user for user in facts["approved_by"] if user != owner]
        person = self.cfg["human_review"]["enabled"]
        need = self.cfg["people"]["human_reviewer"] if person else ""
        # Without a human review, the reviewer agent's pass is the approval.
        approver = (need if need in others else "") if person else REVIEWER_AGENT
        if not approver:
            return None
        if person and ticket.get("approved_sha") and ticket["approved_sha"] != facts["sha"]:
            why = f"New commits were pushed to !{iid} after {self._name(approver)} approved it. Ask for another look before it goes for merge."
            return self._needs_owner(state, ticket, why)
        ticket["approved_sha"] = facts["sha"]
        if facts["conflicts"]:
            return self._back_to_developer(state, ticket, MSG["conflict"].format(iid=iid, target=target, key=key))
        if facts["pipeline_on_head"] and facts["pipeline"] in ("failed", "canceled"):
            return self._back_to_developer(state, ticket, MSG["pipeline"].format(iid=iid))
        ready = (
            facts["pipeline"] == "success"
            and facts["pipeline_on_head"]
            and not facts["draft"]
            and facts["threads_resolved"]
        )
        if not ready:
            return None

        people = self.cfg["people"]
        merger, merger_name = people["merger"], people["merger_name"]
        self._move(state, ticket, "reviewed")
        self._do(f"Assign !{iid} to {merger_name} for merge", lambda: self.gitlab.set_assignees(iid, [merger]))
        note = stamp("manager", MSG["ask_merge"].format(user=merger, key=key, approver=self._name(approver)))
        self._do(f"Tell {merger_name} that !{iid} is ready", lambda: self.gitlab.comment(iid, note))
        text = f"Approved by {self._name(approver)}, pipeline passed, every thread resolved. Assigned to you for merge."
        add_event(state, text, key, to=merger)
        return self._set(state, ticket, "waiting_merge", merger)

    def _merged(self, state, ticket):
        self._move(state, ticket, "deployed")
        ticket["todo"] = None
        text = f"!{ticket['mr_iid']} is merged. {ticket['key']} is deployed to staging and its developer session is closed."
        self._note(state, ticket, text)
        return self._set(state, ticket, "on_staging", "")


# ---------------------------------------------------------------------- answers for the owner


def lanes(state):
    """The office's own tickets, grouped by who holds them."""
    grouped = {name: [] for name in ("owner", "developer", "reviewer", "human_reviewer", "merger", "staging")}
    for key in state["order"]:
        ticket = state["tickets"][key]
        grouped[LANE[ticket["state"]]].append(ticket)
    return grouped


def _tag(ticket):
    mr = f" !{ticket['mr_iid']}" if ticket.get("mr_iid") else ""
    return f"{ticket['key']}{mr} ({LABEL[ticket['state']].lower()})"


def brief(state, cfg):
    """A plain status for the owner. Only the office's own tickets and merge requests appear."""
    people = cfg["people"]
    grouped = lanes(state)
    active = state.get("active")
    lines = []
    if active:
        lines.append(f"Working now: the {active['role']} on {active['ticket']}. {active.get('last', '')}".strip())
    else:
        lines.append("Nobody is working right now.")
    names = [
        ("owner", "Waiting for you"),
        ("developer", "With the developer"),
        ("reviewer", "With the reviewer agent"),
        ("human_reviewer", f"With {HUMAN_REVIEWER}"),
        ("merger", f"With {people['merger_name']} for merge"),
    ]
    for lane, title in names:
        tickets = grouped[lane]
        if tickets:
            lines.append(f"{title}: {len(tickets)}. " + ", ".join(_tag(ticket) for ticket in tickets))
    lines.append(f"On staging: {len(grouped['staging'])}.")
    for ticket in grouped["owner"]:
        detail = (ticket.get("question") or {}).get("text") or ticket.get("error") or ""
        lines.append(f"{ticket['key']} needs you: {detail}")
    return "\n".join(lines)


def answer_question(question, state, cfg):
    """Answer the owner from the task list. Counts cover the office's own merge requests only."""
    people = cfg["people"]
    grouped = lanes(state)
    asked = question.lower()
    first = []

    def words(username, name):
        return ({username.lower()} | {part.lower() for part in name.replace(".", "").split() if len(part) > 3}) - {""}

    merger_words = words(people["merger"], people["merger_name"])
    reviewer_words = words(people["human_reviewer"], HUMAN_REVIEWER)
    if any(word in asked for word in merger_words):
        tickets = grouped["merger"]
        listed = ", ".join(_tag(ticket) for ticket in tickets) or "none"
        first.append(f"{len(tickets)} of the office's merge requests are with {people['merger_name']}: {listed}.")
    if any(word in asked for word in reviewer_words) or "review" in asked:
        agent, person = grouped["reviewer"], grouped["human_reviewer"]
        desk = f"With the reviewer agent: {', '.join(_tag(t) for t in agent) or 'none'}. " if agent or cfg["agent_review"]["enabled"] else ""
        first.append(f"{desk}With {HUMAN_REVIEWER}: {', '.join(_tag(t) for t in person) or 'none'}.")
    if "block" in asked or "stuck" in asked or "need" in asked:
        waiting = grouped["owner"]
        first.append(f"{len(waiting)} ticket(s) are waiting for you.")
    return "\n".join(first + ["", brief(state, cfg)] if first else [brief(state, cfg)])
