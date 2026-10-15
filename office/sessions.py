"""Starting the role sessions: by hand, or through the Claude CLI."""

import json
import os
import shlex
import subprocess
import threading
import time
import uuid

from office.config import HOME, PLACEHOLDER, skill_text, state_dir
from office.state import add_report, now

SKILL_COMMAND = {
    "developer": "/office-developer",
    "coauthor": "/office-coauthor",
    "reviewer": "/office-reviewer",
    "manager": "/office-manager",
}


class ManualLauncher:
    """The owner runs each role's skill himself. The manager only records what is next."""

    kind = "manual"

    def busy(self):
        return False

    def launch(self, _state, _ticket, _todo):
        return False


def describe(event):
    """One short line for the control page, from a Claude CLI stream-json event."""
    kind = event.get("type")
    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                name, args = block.get("name", "tool"), block.get("input") or {}
                detail = args.get("command") or args.get("file_path") or args.get("pattern") or args.get("description")
                first = str(detail or "").strip().splitlines()
                return f"{name}: {first[0][:110]}" if first else name
            if block.get("type") == "text" and (block.get("text") or "").strip():
                return block["text"].strip().splitlines()[0][:140]
    if kind == "result":
        return "Stopped with an error" if event.get("is_error") else "Finished"
    return ""


GUARD_SETTINGS = HOME / "guards" / "session-settings.json"


def settings_file(cfg):
    """The guard settings with this folder's real path in them, written beside the task list."""
    path = state_dir(cfg) / "session-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GUARD_SETTINGS.read_text(encoding="utf-8").replace(PLACEHOLDER, str(HOME)), encoding="utf-8")
    return path


# Sessions never get a credential in their environment. officectl reads the env file itself.
HELD_BACK = ("JIRA_", "GITLAB_")
# Nor any variable whose name says it holds a secret.
SECRET_NAME = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "PRIVATE_KEY")


class ClaudeLauncher:
    """Runs one role session at a time through the Claude CLI and follows what it is doing.

    Every session runs behind the office guard: the settings file makes Claude Code refuse
    whatever is not allowed and call guards/guard.py before each tool use. Only the backend's
    auto mode builds this class, and it starts nothing until the owner presses Start.
    """

    kind = "auto"

    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self._thread = None

    def busy(self):
        return bool(self._thread and self._thread.is_alive())

    def command(self, ticket, todo):
        """The CLI call for this message, the session it runs under, and where it runs."""
        role, claude = todo["role"], self.cfg["claude"]
        known = ticket["sessions"].get(role)
        resume = bool(known) and not todo.get("fresh")
        session = known if resume else str(uuid.uuid4())
        where = ticket["worktree"]
        prompt = (
            f"Message from the manager about {ticket['key']}:\n\n{todo['message']}\n\n"
            f"Working copy: {where}\nOffice tool: python3 {HOME}/officectl.py"
        )
        cmd = shlex.split(claude["bin"]) + ["-p", prompt, "--output-format", "stream-json", "--verbose"]
        cmd += ["--append-system-prompt", skill_text(role)]
        cmd += ["--resume", session] if resume else ["--session-id", session]
        cmd += ["--add-dir", str(HOME), "--add-dir", str(state_dir(self.cfg))]
        # Nobody is watching: refuse what is not allowed, and ask the guard before every tool use.
        cmd += ["--settings", str(settings_file(self.cfg)), "--permission-mode", "dontAsk"]
        if claude.get("model"):
            cmd += ["--model", claude["model"]]
        if claude.get("max_budget_usd"):
            cmd += ["--max-budget-usd", str(claude["max_budget_usd"])]
        return cmd + list(claude.get("extra_args") or []), session, where

    def launch(self, state, ticket, todo):
        role, key = todo["role"], ticket["key"]
        if not ticket.get("worktree") or not GUARD_SETTINGS.exists():
            # Never in the owner's own checkout, and never without the guard.
            why = "No session was started: the ticket has no working copy of its own, or the guard settings are missing."
            add_report(state, key, role, "failed", note=why)
            return False
        cmd, session, where = self.command(ticket, todo)
        ticket["sessions"][role] = session
        state["active"] = {"role": role, "ticket": key, "since": now(), "last": "Starting"}
        env = {name: value for name, value in os.environ.items() if not name.startswith(HELD_BACK) and not any(word in name for word in SECRET_NAME)}
        env.update(OFFICE_ROLE=role, OFFICE_TICKET=key, OFFICE_HOME=str(HOME), OFFICE_WORKTREE=where)
        args = (key, role, cmd, where, env, state["seq"])
        self._thread = threading.Thread(target=self._run, args=args, daemon=True)
        self._thread.start()
        return True

    def _run(self, key, role, cmd, where, env, since):
        logs = state_dir(self.cfg) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        code, cost, last_shown = -1, 0.0, 0.0
        limit = float(self.cfg["claude"].get("max_minutes") or 0) * 60
        try:
            with (
                open(logs / f"{key}-{role}.jsonl", "a", encoding="utf-8") as log,
                subprocess.Popen(
                    cmd,
                    cwd=where,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                ) as proc,
            ):
                # A session that runs past the limit is stopped, and the ticket then stops at the owner.
                stopper = threading.Timer(limit, proc.kill) if limit else None
                if stopper:
                    stopper.daemon = True
                    stopper.start()
                for line in proc.stdout:
                    log.write(line)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "result":
                        cost = float(event.get("total_cost_usd") or 0)
                    text = describe(event)
                    if text and time.monotonic() - last_shown > 1:
                        last_shown = time.monotonic()
                        self.store.update(lambda state, said=text: _show(state, key, said))
                code = proc.wait()
                if stopper:
                    stopper.cancel()
        except OSError as exc:
            said = f"Could not start the Claude CLI: {exc}"
            self.store.update(lambda state: _show(state, key, said))
        self.store.update(lambda state: _finish(state, key, role, since, code, cost))


def _show(state, key, text):
    if state.get("active") and state["active"]["ticket"] == key:
        state["active"]["last"] = text


def _finish(state, key, role, since, code, cost):
    """The session ended. If it left no report, say so, so the ticket does not sit there silently."""
    if state.get("active") and state["active"]["ticket"] == key:
        state["active"] = None
    ticket = state["tickets"].get(key)
    if not ticket:
        return
    ticket["running"] = ""
    ticket["cost_usd"] = round(ticket.get("cost_usd", 0) + cost, 4)
    reported = any(r["ticket"] == key and r["role"] == role and r["id"] > since for r in state["reports"])
    if not reported:
        note = f"The session ended without a report (exit code {code})."
        add_report(state, key, role, "failed", note=note)
