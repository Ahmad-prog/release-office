#!/usr/bin/env python3
"""officectl: the one tool every role in the Release Office uses.

Agents use it to read their message, read the ticket, talk to GitLab and report to the
manager. The owner uses it to see the task list and to step the manager by hand.

    python3 officectl.py status
    python3 officectl.py inbox --ticket PROJ-105
    python3 officectl.py report --ticket PROJ-105 --role developer --status built --note "..."

Tokens come from the environment (JIRA_EMAIL, JIRA_TOKEN, GITLAB_TOKEN) and are never printed.
Set OFFICE_DRY=1 to see what a write would do without sending it.
`officectl run -- <command>` runs a command with the other credentials stored for it in its environment.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from office import config, context, plan
from office.api import ApiError
from office.facts import classify, describe_threads, stamp
from office.gitlab import GitLab
from office.gitops import GitError, fetch, git, push
from office.jira import Jira
from office.manager import LABEL, LANE, Manager, brief
from office.sessions import SKILL_COMMAND, ManualLauncher
from office.state import Store, add_report

REPORTS = {
    "developer": {"blocked", "built", "mr_open", "fixed", "failed"},
    "coauthor": {"clean", "findings", "failed"},
    "reviewer": {"threads_posted", "passed", "failed"},
}
# What each role may run. A person (no OFFICE_ROLE) and the manager may run anything.
ALLOWED = {
    "developer": {"status", "inbox", "ticket", "ticket-check", "report", "mr", "run", "fetch", "push", "mr-create", "threads", "thread-reply", "comment"},
    "coauthor": {"status", "inbox", "ticket", "ticket-check", "report"},
    "reviewer": {"status", "inbox", "ticket", "ticket-check", "report", "mr", "run", "fetch", "threads", "thread-post", "thread-reply", "thread-resolve", "comment"},
}
# The planner works in the owner's own conversation, not in a guarded session, and only plans.
PLANNER = {"status", "ticket", "ticket-check", "ticket-example", "ticket-create", "ticket-update", "run", "fetch"}
SESSION_URL = "claude.ai/code/session"
TICKET_ID = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def fail(text):
    sys.exit(f"officectl: {text}")


class Office:
    """Config, state and the two clients, built once per command."""

    def __init__(self):
        self.cfg = config.load()
        self.store = Store(config.state_dir(self.cfg))
        self.role = os.environ.get("OFFICE_ROLE", "")
        self.dry = os.environ.get("OFFICE_DRY", "") not in ("", "0")
        self._jira = self._gitlab = None

    def need_tokens(self):
        missing = config.missing_tokens()
        if missing:
            fail(f"missing {', '.join(missing)}. Put them in {config.env_file()} (NAME=value per line) or export them")
        return config.tokens()

    @property
    def jira(self):
        if not self._jira:
            tokens = self.need_tokens()
            self._jira = Jira(self.cfg["jira"]["base_url"], tokens["jira_email"], tokens["jira_token"])
        return self._jira

    @property
    def gitlab(self):
        if not self._gitlab:
            tokens = self.need_tokens()
            gitlab = self.cfg["gitlab"]
            self._gitlab = GitLab(gitlab["base_url"], gitlab["project_id"], tokens["gitlab_token"])
        return self._gitlab

    def manager(self):
        token = config.tokens()["gitlab_token"]
        return Manager(self.cfg, self.store, self.jira, self.gitlab, ManualLauncher(), token)

    def ticket(self, key):
        ticket = self.store.read()["tickets"].get(key)
        if not ticket:
            fail(f"{key} is not on the office's list. Add it with: officectl add {key}")
        return ticket

    def own_mr(self, iid):
        """Agents write only on merge requests the office opened, and an unattended one only on its own."""
        mine = os.environ.get("OFFICE_TICKET", "")
        owned = {t["mr_iid"]: key for key, t in self.store.read()["tickets"].items() if t.get("mr_iid")}
        if iid not in owned or (mine and owned[iid] != mine):
            whose = f"the merge request for {mine}" if mine else "one of the office's merge requests"
            fail(f"!{iid} is not {whose}, so the office will not write on it")

    def write(self, text, fn):
        """Do an outside write, or only describe it when OFFICE_DRY is set."""
        if self.dry:
            print(f"[dry run] would {text}")
            return None
        return fn()


def read_body(args):
    if getattr(args, "body_file", None):
        return Path(args.body_file).read_text(encoding="utf-8").strip()
    if getattr(args, "body", None):
        return args.body.strip()
    fail("give the text with --body-file or --body")
    return ""


# ---------------------------------------------------------------------- for everyone


def cmd_status(office, args):
    state = office.store.read()
    if args.json:
        print(json.dumps(state, indent=1))
        return
    print(f"Mode: {state['mode']} · {'open' if state['running'] else 'closed'}")
    print(brief(state, office.cfg))
    for key in state["order"]:
        ticket = state["tickets"][key]
        todo = ticket.get("todo")
        if todo and not ticket["running"]:
            print(f"\nNext for {key}: {SKILL_COMMAND[todo['role']]} {key}\n  {todo['message'].splitlines()[0]}")


def cmd_inbox(office, args):
    ticket = office.ticket(args.ticket)
    role = args.role or office.role
    print(f"{ticket['key']} · {LABEL[ticket['state']]} · {ticket['summary']}")
    print(f"Branch: {ticket['branch']} · Working copy: {ticket['worktree'] or office.cfg['repo']['path']}")
    if ticket.get("mr_iid"):
        print(f"Merge request: !{ticket['mr_iid']} {ticket.get('mr_url', '')}")
    print(f"Notes folder: {config.ticket_dir(office.cfg, ticket['key'])}")
    todo = ticket.get("todo")
    if todo and (not role or todo["role"] == role):
        print(f"\nMessage from the manager to the {todo['role']}:\n{todo['message']}")
    else:
        print("\nNo message is waiting" + (f" for the {role}." if role else "."))


def cmd_ticket(office, args):
    print(office.jira.ticket_text(args.key))
    # The owner's answers are kept in the office's notes, not on Jira. They bind like the ticket does.
    kept = config.state_dir(office.cfg) / "tickets" / args.key / "decisions.md"
    if kept.exists():
        print(f"\n--- The owner's answers to the developer (kept by the office, not on Jira) ---\n{kept.read_text(encoding='utf-8')}")


def repo_facts(office, text):
    """Look up every path the ticket names on the target branch. Returns the faults and what was checked."""
    repo, branch = office.cfg["repo"]["path"], office.cfg["gitlab"]["target_branch"]
    ref, stale = f"origin/{branch}", ""
    try:
        fetch(repo, branch, config.tokens()["gitlab_token"], str(config.state_dir(office.cfg)))
    except (GitError, OSError):
        stale = ", as last fetched (the fetch failed)"
    try:
        tops = set(git(["ls-tree", "--name-only", ref], repo).stdout.splitlines())
        sha = git(["rev-parse", "--short", ref], repo).stdout.strip()
    except (GitError, OSError) as exc:
        return [f"The paths could not be checked: {repo} has no {ref} to read ({exc})."], ""
    exists = lambda path: git(["cat-file", "-e", f"{ref}:{path}"], repo, check=False).returncode == 0  # noqa: E731
    problems, count = plan.check_repo(text, tops, exists, branch, office.cfg["repo"].get("protected_paths") or ())
    return problems, f"{count} path(s) looked up on {branch} at {sha}{stale}"


def cmd_ticket_check(office, args):
    """Is this ticket planned well enough for the office to run it?"""
    if not args.key and not args.file:
        fail("give a ticket key, or --file with a draft")
    text = Path(args.file).read_text(encoding="utf-8") if args.file else office.jira.description_text(args.key)
    problems, looked = plan.check(text), ""
    if plan.has_plan(text):
        wrong, looked = repo_facts(office, text)
        problems += wrong
    if problems:
        print("Not ready for the office:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(f"Ready for the office. {looked}.")
    print("That covers the shape and the paths. Whether each fact and each check is true is for whoever wrote it to have verified.")


def cmd_ticket_example(_office, _args):
    """A complete ticket in the shape the office runs."""
    print(plan.EXAMPLE)


def _ready(office, args, body):
    """Refuse a plan that is not ready. A ready one comes back with the line saying what its paths were checked against."""
    problems, looked = plan.check(body), ""
    if plan.has_plan(body):
        wrong, looked = repo_facts(office, body)
        problems += wrong
    if not problems:
        return plan.stamped(body, looked)
    listed = "\n".join(f"  - {problem}" for problem in problems)
    if not args.draft:
        fail(f"the ticket is not ready for the office:\n{listed}\nFix it. Only the owner may pass --draft to write it anyway.")
    if office.role:
        fail("--draft writes a ticket that is not ready. Only the owner may do that, by running the command himself.")
    return body


def cmd_ticket_create(office, args):
    """A new, planned ticket: assigned to the owner, in the current sprint, waiting in To Do."""
    summary, jira = args.summary.strip(), office.cfg["jira"]
    body = _ready(office, args, read_body(args))

    def create():
        sprint = office.jira.sprint_id(jira["board_id"], jira["sprint"])
        return office.jira.create(jira["project"], args.type, summary, body, jira["sprint_field"], sprint, args.epic or "")

    key = office.write(f"create a {args.type} in {jira['project']}, sprint {jira['sprint']}, titled: {summary}", create)
    if key:
        print(f"Created {key}: {office.jira.url(key)}\nIt is in To Do. The office takes it when you move it to In Progress.")


def cmd_ticket_update(office, args):
    """Replace a ticket's description with the plan. What it said before is kept underneath."""
    body = _ready(office, args, read_body(args))
    merged = plan.with_original(body, office.jira.description_text(args.key))
    kept = " The earlier text is kept under \"Original description\"." if merged != body else ""
    done = office.write(f"replace the description of {args.key}", lambda: office.jira.update(args.key, args.summary or "", merged) or True)
    if done:
        print(f"Updated {args.key}: {office.jira.url(args.key)}.{kept}")


def cmd_report(office, args):
    role = args.role or office.role
    if role not in REPORTS:
        fail("say which role is reporting with --role developer|coauthor|reviewer")
    if args.status not in REPORTS[role]:
        fail(f"the {role} can report: {', '.join(sorted(REPORTS[role]))}")
    if args.status == "blocked" and not args.question:
        fail("a blocked report needs --question")
    if args.status == "mr_open" and not args.mr:
        fail("an mr_open report needs --mr")
    office.ticket(args.ticket)
    extra = {"note": args.note, "question": args.question, "mr": args.mr, "count": args.count, "file": args.file, "design": args.design or None, "drift": args.drift or None}
    office.store.update(lambda state: add_report(state, args.ticket, role, args.status, **extra))
    print(f"Reported to the manager: {role} · {args.status}")


# ---------------------------------------------------------------------- git and GitLab


def cmd_fetch(office, args):
    tokens = office.need_tokens()
    fetch(os.getcwd(), args.branch, tokens["gitlab_token"], str(config.state_dir(office.cfg)))
    print(f"Fetched origin/{args.branch}")


def cmd_push(office, args):
    ticket = office.ticket(args.ticket)
    tokens = office.need_tokens()
    where = str(config.state_dir(office.cfg))
    text = f"push the current branch to {ticket['branch']}"
    out = office.write(text, lambda: push(os.getcwd(), ticket["branch"], tokens["gitlab_token"], where, args.force_with_lease))
    if out is not None:
        print(out or "Pushed.")


def cmd_mr_create(office, args):
    ticket = office.ticket(args.ticket)
    key, target = ticket["key"], office.cfg["gitlab"]["target_branch"]
    title = args.title.strip()
    description = read_body(args)
    # The merge commit already carries the branch name, so the title must not repeat the ticket.
    if TICKET_ID.search(title):
        fail("the title must not carry a ticket number. Put the ticket in the description.")
    if SESSION_URL in description:
        fail("the description must not carry a Claude session link")
    if key not in description:
        fail(f"the description must name the ticket ({key})")
    existing = office.gitlab.mr_for_branch(ticket["branch"])
    if existing and existing.get("state") == "opened":
        print(f"!{existing['iid']} already exists for {ticket['branch']}: {existing['web_url']}")
        return
    made = office.write(
        f"open a merge request from {ticket['branch']} into {target} titled: {title}",
        lambda: office.gitlab.create_mr(ticket["branch"], target, title, description),
    )
    if made:
        print(f"Opened !{made['iid']}: {made['web_url']}\nNow report it: officectl report --ticket {key} --status mr_open --mr {made['iid']}")


def cmd_threads(office, args):
    threads = describe_threads(office.gitlab.discussions(args.mr))
    if args.mine:
        threads = [t for t in threads if t["opened_by"] == "reviewer"]
    if args.people:
        threads = [t for t in threads if t["opened_by"] == "person"]
    if args.open:
        threads = [t for t in threads if not t["resolved"]]
    if args.json:
        print(json.dumps(threads, indent=1))
        return
    if not threads:
        print("No threads match.")
    for thread in threads:
        kind = "thread" if thread["resolvable"] else "comment"
        where = f" · {thread['path']}:{thread['line']}" if thread["path"] else ""
        status = "resolved" if thread["resolved"] else "open"
        owner = "reviewer agent" if thread["opened_by"] == "reviewer" else config.shown(office.cfg, thread["author"])
        print(f"\n[{thread['id']}] {kind} by {owner} · {status}{where}")
        for note in thread["notes"]:
            body = " ".join(note["body"].split())
            print(f"   {config.shown(office.cfg, note['author'])}: {body[:400]}")


def cmd_thread_post(office, args):
    office.own_mr(args.mr)
    body = stamp("reviewer", read_body(args))
    position = None
    if args.file and args.line:
        position = office.gitlab.position(args.mr, args.file, args.line)

    def post():
        try:
            return office.gitlab.post_thread(args.mr, body, position)
        except ApiError as exc:
            if not position or exc.status != 400:
                raise
            # The line is not part of the diff, so anchor the thread to the merge request instead.
            anchored = stamp("reviewer", f"`{args.file}:{args.line}`\n\n{read_body(args)}")
            return office.gitlab.post_thread(args.mr, anchored)

    made = office.write(f"post a thread on !{args.mr}" + (f" at {args.file}:{args.line}" if position else ""), post)
    if made:
        print(f"Posted thread {made['id']}")


def cmd_thread_reply(office, args):
    office.own_mr(args.mr)
    role = office.role if office.role in ("developer", "reviewer") else (args.role or "developer")
    body = stamp(role, read_body(args))
    made = office.write(f"reply on thread {args.thread} of !{args.mr}", lambda: office.gitlab.reply(args.mr, args.thread, body))
    if made:
        print("Replied.")


def cmd_thread_resolve(office, args):
    office.own_mr(args.mr)
    threads = {t["id"]: t for t in describe_threads(office.gitlab.discussions(args.mr))}
    thread = threads.get(args.thread)
    if not thread:
        fail(f"no thread {args.thread} on !{args.mr}")
    if thread["opened_by"] != "reviewer":
        fail("this thread was not opened by the reviewer agent. A person's thread is that person's to resolve.")
    receipt = stamp("reviewer", read_body(args))

    def resolve():
        office.gitlab.reply(args.mr, args.thread, receipt)
        return office.gitlab.resolve(args.mr, args.thread)

    if office.write(f"resolve thread {args.thread} of !{args.mr}", resolve):
        print("Resolved.")


def cmd_comment(office, args):
    office.own_mr(args.mr)
    role = office.role if office.role in ("developer", "reviewer") else (args.role or "developer")
    body = stamp(role, read_body(args))
    if office.write(f"comment on !{args.mr}", lambda: office.gitlab.comment(args.mr, body)):
        print("Commented.")


def cmd_mr(office, args):
    facts = classify(office.gitlab.discussions(args.mr))
    mr = office.gitlab.mr(args.mr)
    pipeline = mr.get("head_pipeline") or {}
    print(f"!{args.mr} · {mr['state']} · {mr['source_branch']} into {mr['target_branch']} · {mr.get('web_url', '')}")
    print(f"Pipeline: {pipeline.get('status', 'none')} · on head commit: {pipeline.get('sha') == mr.get('sha')}")
    print(f"Approved by: {', '.join(config.shown(office.cfg, user) for user in office.gitlab.approvals(args.mr)) or 'nobody'}")
    print(f"Reviewer agent threads: {facts['agent_open']} open of {facts['agent_total']} · people's open threads: {facts['people_open']}")


def cmd_run(_office, args):
    """Run a command with the owner's other stored credentials (a database or data-tool login) in its environment.

    The Jira and GitLab tokens and the database login are held back: only officectl itself uses those.
    """
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        fail("give the command after --, for example: officectl run -- uv run python probe.py")
    passed = {name: value for name, value in config.stored_env().items() if not name.startswith(("JIRA_", "GITLAB_"))}
    try:
        os.execvpe(command[0], command, {**passed, **os.environ})
    except OSError as exc:
        fail(f"could not run {command[0]}: {exc}")



# ---------------------------------------------------------------------- for the owner and the manager


def cmd_tick(office, args):
    for line in office.manager().tick(dry=args.dry) or ["Nothing to do."]:
        print(("would: " if args.dry else "") + line)


def cmd_start(office, _args):
    office.manager().start()
    print("The office is open.")


def cmd_stop(office, _args):
    office.manager().stop()
    print("The office is closed.")


def cmd_add(office, args):
    added = office.manager().add(args.key, adopt=args.adopt, unplanned=args.unplanned)
    print(f"{args.key} is on the list." if added else f"{args.key} was already on the list.")


def cmd_answer(office, args):
    if not office.manager().answer(args.ticket, args.text, record=args.record):
        fail(f"{args.ticket} is not waiting for an answer")
    where = " and writes it on the Jira ticket as a core design decision" if args.record else ""
    print(f"Answer recorded. The next manager step passes it to the developer{where}.")


def cmd_context(office, args):
    """Everything recorded about a ticket that is waiting on the owner."""
    parts = context.build(office.cfg, office.store.read(), args.key, office.jira)
    if not parts:
        fail(f"{args.key} is not on the office's list")
    for part in parts:
        print(f"\n## {part['title']}\n{part['text']}")


def cmd_followup(office, args):
    if not office.manager().follow_up(args.ticket, args.text):
        fail(f"{args.ticket} is not waiting on a question")
    print("Recorded. The next manager step asks the developer for it.")


def cmd_resume(office, args):
    if not office.manager().resume(args.key, args.state or "", args.note or "", args.unplanned):
        fail(f"{args.key} is not stuck, or the state is unknown. States: {', '.join(sorted(LANE))}")
    print(f"{args.key} is back in the flow as: {LABEL[office.ticket(args.key)['state']].lower()}")


def cmd_forget(office, args):
    """Take a ticket off the list, to start it fresh. Its branch, working copy and notes are left alone."""
    if office.ticket(args.key)["running"]:
        fail(f"a session is working on {args.key}. Close the office, wait for it to finish, then try again")

    def drop(state):
        state["tickets"].pop(args.key, None)
        state["order"] = [key for key in state["order"] if key != args.key]
        state["reports"] = [report for report in state["reports"] if report["ticket"] != args.key]
        state["seen"].pop(args.key, None)

    office.store.update(drop)
    print(f"{args.key} is off the list. Remove its branch and working copy as README.md says, then press Start to take it fresh.")


def build_parser():
    parser = argparse.ArgumentParser(prog="officectl", description="The Release Office tool.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, fn, text):
        made = sub.add_parser(name, help=text)
        made.set_defaults(fn=fn)
        return made

    add("status", cmd_status, "the task list and what is next").add_argument("--json", action="store_true")
    inbox = add("inbox", cmd_inbox, "your message from the manager")
    inbox.add_argument("--ticket", required=True)
    inbox.add_argument("--role")
    add("ticket", cmd_ticket, "print the Jira ticket").add_argument("key")
    checking = add("ticket-check", cmd_ticket_check, "is the ticket planned well enough for the office?")
    checking.add_argument("key", nargs="?")
    checking.add_argument("--file", help="check a draft instead of a Jira ticket")
    add("ticket-example", cmd_ticket_example, "print a complete planned ticket")
    creating = add("ticket-create", cmd_ticket_create, "create a planned ticket in Jira")
    creating.add_argument("--summary", required=True)
    creating.add_argument("--type", default="Story", choices=("Story", "Task", "Bug"))
    creating.add_argument("--epic", help="the parent epic's key")
    updating = add("ticket-update", cmd_ticket_update, "replace a ticket's description with the plan")
    updating.add_argument("key")
    updating.add_argument("--summary")
    for made in (creating, updating):
        made.add_argument("--body-file", "--description-file", dest="body_file", required=True)
        made.add_argument("--draft", action="store_true", help="write it even though the plan check fails")

    report = add("report", cmd_report, "report to the manager")
    report.add_argument("--ticket", required=True)
    report.add_argument("--role")
    report.add_argument("--status", required=True)
    report.add_argument("--note")
    report.add_argument("--question")
    report.add_argument("--mr", type=int)
    report.add_argument("--count", type=int)
    report.add_argument("--file")
    report.add_argument("--design", action="store_true", help="with blocked: the answer would change a core design decision")
    report.add_argument("--drift", action="store_true", help="with findings (co-author): the change does not match the ticket's scope")

    add("fetch", cmd_fetch, "git fetch from origin").add_argument("--branch", default="staging")
    pushing = add("push", cmd_push, "push the ticket's branch")
    pushing.add_argument("--ticket", required=True)
    pushing.add_argument("--force-with-lease", action="store_true")

    create = add("mr-create", cmd_mr_create, "open the merge request")
    create.add_argument("--ticket", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--body-file", "--description-file", dest="body_file", required=True)

    threads = add("threads", cmd_threads, "list the threads on a merge request")
    threads.add_argument("--mr", type=int, required=True)
    threads.add_argument("--open", action="store_true")
    threads.add_argument("--mine", action="store_true", help="only the reviewer agent's threads")
    threads.add_argument("--people", action="store_true", help="only people's threads")
    threads.add_argument("--json", action="store_true")
    add("mr", cmd_mr, "a merge request at a glance").add_argument("--mr", type=int, required=True)

    for name, fn, text in (
        ("thread-post", cmd_thread_post, "post a review thread (reviewer)"),
        ("thread-reply", cmd_thread_reply, "reply on a thread"),
        ("thread-resolve", cmd_thread_resolve, "resolve one of the reviewer agent's own threads (reviewer)"),
        ("comment", cmd_comment, "a plain comment on the merge request"),
    ):
        made = add(name, fn, text)
        made.add_argument("--mr", type=int, required=True)
        made.add_argument("--body-file")
        made.add_argument("--body")
        made.add_argument("--role", choices=("developer", "reviewer"))
        if name in ("thread-reply", "thread-resolve"):
            made.add_argument("--thread", required=True)
        if name == "thread-post":
            made.add_argument("--file")
            made.add_argument("--line", type=int)

    running = add("run", cmd_run, "run a command with the other stored credentials in its environment")
    running.add_argument("command", nargs=argparse.REMAINDER)
    add("tick", cmd_tick, "run one manager step").add_argument("--dry", action="store_true", help="only say what it would do")
    add("start", cmd_start, "open the office")
    add("stop", cmd_stop, "close the office")
    adding = add("add", cmd_add, "put a ticket on the list by hand")
    adding.add_argument("key")
    adding.add_argument("--adopt", action="store_true", help="the branch already exists and is yours")
    adding.add_argument("--unplanned", action="store_true", help="start it as it is, without the plan check")
    answer = add("answer", cmd_answer, "answer a blocked ticket")
    answer.add_argument("--ticket", required=True)
    answer.add_argument("--text", required=True)
    answer.add_argument("--record", action="store_true", help="it changes a core design decision: write it on the Jira ticket")
    add("context", cmd_context, "everything recorded about a ticket that waits on the owner").add_argument("key")
    followup = add("followup", cmd_followup, "ask the developer for more context before answering")
    followup.add_argument("--ticket", required=True)
    followup.add_argument("--text", required=True)
    resume = add("resume", cmd_resume, "put a stuck ticket back in the flow")
    resume.add_argument("key")
    resume.add_argument("--state", help="defaults to where the ticket stopped")
    resume.add_argument("--note")
    resume.add_argument("--unplanned", action="store_true", help="start it without a complete plan")
    add("forget", cmd_forget, "take a ticket off the list, to start it fresh").add_argument("key")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    office = Office()
    allowed = {**ALLOWED, "planner": PLANNER}
    if office.role in allowed and args.cmd not in allowed[office.role]:
        fail(f"the {office.role} does not run `{args.cmd}`. That belongs to another role.")
    try:
        args.fn(office, args)
    except (ApiError, GitError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
