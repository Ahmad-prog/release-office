#!/usr/bin/env python3
"""The Release Office backend: keeps the task list and serves the control page.

    python3 server.py --mode demo     pretend Jira, GitLab and agents. Touches nothing.
    python3 server.py --mode manual   real Jira and GitLab. You start each role's skill yourself,
                                      and the manager moves only when you press "Run one step".
    python3 server.py --mode auto     real Jira and GitLab. After you press Start the manager moves
                                      on its own and starts each role's session behind the guard.

Then open http://127.0.0.1:8765. Tokens come from JIRA_EMAIL, JIRA_TOKEN and GITLAB_TOKEN.
Only auto mode starts agent sessions, and only once the owner has pressed Start.
"""

import argparse
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from office import config, context, plan
from office.api import ApiError
from office.demo import DemoLauncher, FakeGitLab, FakeJira
from office.gitlab import GitLab
from office.jira import Jira
from office.manager import LABEL, LANE, Manager, answer_question
from office.sessions import SKILL_COMMAND, ClaudeLauncher, ManualLauncher
from office.state import Store

LOCAL = {"127.0.0.1", "localhost"}


class App:
    _looked, _upcoming = 0.0, ()

    def __init__(self, cfg, mode):
        self.cfg, self.mode = cfg, mode
        root = config.state_dir(cfg)
        if mode == "demo":
            # The demo keeps its own task list and starts clean, so it never mixes with real work.
            root = root / "demo"
            shutil.rmtree(root, ignore_errors=True)
            cfg["state_dir"] = str(root)
            cfg["people"]["human_reviewer"] = "human.reviewer"  # a pretend account, like everything else here
        self.store = Store(root)
        self.jira, self.gitlab, token = self._clients()
        pace = float(os.environ.get("OFFICE_DEMO_PACE", "1.1"))
        if mode == "demo":
            launcher = DemoLauncher(cfg, self.store, self.gitlab, pace)
        elif mode == "auto":
            launcher = ClaudeLauncher(cfg, self.store)
        else:
            launcher = ManualLauncher()
        self.manager = Manager(cfg, self.store, self.jira, self.gitlab, launcher, token)
        # In manual mode the manager moves only when the owner presses "Run one step".
        self.stepping = mode == "manual"
        # How often the manager looks at Jira and GitLab when nothing has been reported to it.
        self.pace = 1.0 if mode == "demo" else float(cfg["poll_seconds"])
        self.store.update(self._reset)

    def _clients(self):
        if self.mode == "demo":
            return FakeJira(), FakeGitLab(self.cfg["owner"]["gitlab_username"]), ""
        missing = config.missing_tokens()
        if missing:
            where = config.env_file()
            sys.exit(
                f"Missing: {', '.join(missing)}.\nPut them in {where}, one NAME=value per line, then: chmod 600 {where}\n"
                "Or export them in this terminal. To look around without them: python3 server.py --mode demo"
            )
        tokens, gitlab = config.tokens(), self.cfg["gitlab"]
        jira = Jira(self.cfg["jira"]["base_url"], tokens["jira_email"], tokens["jira_token"])
        return jira, GitLab(gitlab["base_url"], gitlab["project_id"], tokens["gitlab_token"]), tokens["gitlab_token"]

    def _reset(self, state):
        state["mode"] = self.mode
        state["active"] = None
        if self.mode == "auto":
            # Sessions start only after the owner presses Start in this run of the backend.
            state["running"] = False
        for ticket in state["tickets"].values():
            ticket["running"] = ""

    def _due(self, seen, last):
        """Something was reported or asked since the last pass, a message waits for a free agent, or it is time to look."""
        state = self.store.read()
        if state["seq"] != seen or time.monotonic() - last >= self.pace:
            return True
        waiting = any(t.get("todo") and not t["running"] and not t["todo"].get("handed") for t in state["tickets"].values())
        return state["running"] and waiting and not self.manager.launcher.busy()

    def loop(self):
        """The demo and auto mode move on their own. Manual mode waits for "Run one step"."""
        seen, last = -1, 0.0
        while True:
            if not self.stepping and self._due(seen, last):
                self.manager.tick()
                seen, last = self.store.read()["seq"], time.monotonic()
            time.sleep(1.0)

    def upcoming(self):
        """The owner's In Progress tickets the office has not taken yet, and how many faults the ticket check finds in each."""
        jira = self.cfg["jira"]
        if self.mode == "demo" or jira["pickup"] != "all":
            return []
        if time.monotonic() - self._looked < 30 and self._looked:
            return list(self._upcoming)
        self._looked = time.monotonic()
        held = self.store.read()["tickets"]
        try:
            found = [item for item in self.jira.in_progress(jira["sprint"], jira["status"]["in_progress"]) if item["key"] not in held]
            self._upcoming = [
                {
                    "key": item["key"],
                    "summary": item["summary"],
                    "faults": len(plan.check(self.jira.description_text(item["key"]))) if jira.get("require_plan") else 0,
                }
                for item in found[:5]
            ]
        except ApiError:
            pass  # keep what was last seen; the manager's own pass reports an unreachable Jira
        return list(self._upcoming)

    def snapshot(self):
        state, people = self.store.read(), self.cfg["people"]
        reviewer = people["human_reviewer"]

        def role(who):
            # The page shows the human reviewer by role, so the account never reaches it.
            return "human_reviewer" if reviewer and who == reviewer else who

        tickets = []
        for key in state["order"]:
            ticket = dict(state["tickets"][key])
            todo = ticket.get("todo")
            ticket["lane"] = LANE[ticket["state"]]
            ticket["label"] = LABEL[ticket["state"]]
            if ticket["state"] == "human_review" and not self.cfg["human_review"]["enabled"]:
                # No person reviews: the merge request waits at the merge desk for its checks.
                ticket["lane"], ticket["label"] = "merger", "Waiting for checks"
            ticket["waiting_on"] = role(ticket.get("waiting_on", ""))
            if ticket.get("mr"):
                ticket["mr"] = {**ticket["mr"], "approved_by": [role(user) for user in ticket["mr"].get("approved_by") or []]}
            # Only in manual mode does the owner run the skill himself.
            by_hand = self.mode == "manual" and todo and not ticket["running"]
            ticket["next"] = f"{SKILL_COMMAND[todo['role']]} {key}" if by_hand else ""
            tickets.append(ticket)
        return {
            "mode": self.mode,
            "stepping": self.stepping,
            "running": state["running"],
            "active": state["active"],
            "tickets": tickets,
            "events": [{**event, "from": role(event["from"]), "to": role(event["to"])} for event in state["events"][-80:][::-1]],
            "owner": self.cfg["owner"]["name"],
            "sprint": self.cfg["jira"]["sprint"],
            "people": {"merger": people["merger"], "merger_name": people["merger_name"]},
            "human_review": self.cfg["human_review"]["enabled"],
            "agent_review": self.cfg["agent_review"]["enabled"],
            "upcoming": self.upcoming(),
            "demo": self.jira.waiting() if self.mode == "demo" else [],
        }

    # ---- what the control page can ask for

    def act(self, name, data):
        key = str(data.get("ticket") or "").strip().upper()
        if name == "start":
            self.manager.start()
        elif name == "stop":
            self.manager.stop()
        elif name == "tick":
            return {"log": self.manager.tick(dry=bool(data.get("dry")))}
        elif name == "ask":
            return {"answer": answer_question(str(data.get("question") or ""), self.store.read(), self.cfg)}
        elif name == "answer":
            return {"ok": self.manager.answer(key, str(data.get("text") or ""), record=bool(data.get("record")))}
        elif name == "followup":
            return {"ok": self.manager.follow_up(key, str(data.get("text") or ""))}
        elif name == "add":
            return {"ok": bool(key) and self.manager.add(key, adopt=bool(data.get("adopt")), unplanned=bool(data.get("unplanned")))}
        elif name == "resume":
            return {"ok": self.manager.resume(key, str(data.get("state") or ""), str(data.get("note") or ""), bool(data.get("unplanned")))}
        elif name == "demo" and self.mode == "demo":
            self._demo(str(data.get("action") or ""), key)
        else:
            return None
        return {"ok": True}

    def _demo(self, action, key):
        """Play the people: the owner moving a ticket, a reviewer commenting or approving, the merge."""
        people = self.cfg["people"]
        if action == "fix":
            return self.jira.fix(key)
        if action == "in_progress":
            return self.jira.move(key, self.cfg["jira"]["status"]["in_progress"])
        iid = (self.store.read()["tickets"].get(key) or {}).get("mr_iid")
        if not iid:
            return None
        if action == "comment":
            return self.gitlab.person_comments(iid, people["human_reviewer"])
        if action == "approve":
            return self.gitlab.approve(iid, people["human_reviewer"])
        if action == "pushback":
            return self.gitlab.person_comments(iid, people["merger"])
        if action == "merge":
            return self.gitlab.merge(iid)
        return None


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "ReleaseOffice"

    def log_message(self, *_args):
        pass

    def _send(self, code, body, kind="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _local(self):
        return (self.headers.get("Host") or "").split(":")[0] in LOCAL

    def do_GET(self):  # noqa: N802
        if not self._local():
            return self._send(403, {"error": "local use only"})
        path = self.path.split("?")[0]
        if path == "/api/state":
            return self._send(200, self.app.snapshot())
        if path == "/api/context":
            key = (urllib.parse.parse_qs(self.path.partition("?")[2]).get("ticket") or [""])[0].strip().upper()
            app = self.app
            return self._send(200, {"ticket": key, "parts": context.build(app.cfg, app.store.read(), key, app.jira)})
        pages = {"/": config.HOME / "ui" / "index.html", "/plan": config.HOME / "docs" / "release-office.html"}
        if path in pages and pages[path].exists():
            return self._send(200, pages[path].read_bytes(), "text/html")
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        # The custom header keeps another site's page from driving the office through the browser.
        if not self._local() or self.headers.get("X-Office") != "1":
            return self._send(403, {"error": "local use only"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "send JSON"})
        name = self.path.split("?")[0].removeprefix("/api/")
        try:
            result = self.app.act(name, data if isinstance(data, dict) else {})
        except Exception as exc:  # the page should hear about any failure rather than hang
            return self._send(500, {"error": str(exc)})
        return self._send(200, result) if result is not None else self._send(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description="The Release Office backend.")
    parser.add_argument("--mode", choices=("demo", "manual", "auto"), default="demo")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    cfg = config.load()
    app = App(cfg, args.mode)
    Handler.app = app
    threading.Thread(target=app.loop, daemon=True).start()
    port = args.port or cfg["port"]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    how = {
        "demo": "moves on its own (pretend data)",
        "manual": "moves when you press Run one step",
        "auto": "starts agent sessions behind the guard once you press Start",
    }[args.mode]
    print(f"Release Office · {args.mode} mode · the manager {how}\nOpen http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
