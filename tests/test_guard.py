"""What an unattended session may and may not do, checked call by call."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import officectl
from guards import guard
from office import config

KEY = "PROJ-1"
CTL = str(guard.OFFICECTL)


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tree = Path(folder.name).resolve() / "worktree"
        self.state = Path(folder.name).resolve() / "state"
        # The repo's protected paths, as office.config.json would list them.
        saved = guard.SETTINGS["protected_paths"]
        guard.SETTINGS["protected_paths"] = ["restricted", "vendor/lib"]
        self.addCleanup(lambda: guard.SETTINGS.__setitem__("protected_paths", saved))
        for part in ("lib/x/tests", "restricted/data", "vendor/lib", ".git"):
            (self.tree / part).mkdir(parents=True)
        self.notes = self.state / "tickets" / KEY

    def ask(self, role, tool, **args):
        return guard.decide(tool, args, role, KEY, str(self.tree), str(self.tree), str(self.state))

    def bash(self, role, command, **extra):
        return self.ask(role, "Bash", command=command, **extra)

    def allowed(self, role, commands):
        for command in commands:
            ok, reason = self.bash(role, command)
            self.assertTrue(ok, f"{role} should be allowed: {command!r} ({reason})")

    def refused(self, role, commands):
        for command in commands:
            ok, _ = self.bash(role, command)
            self.assertFalse(ok, f"{role} should be refused: {command!r}")


class Developer(Case):
    def test_the_work_it_needs_is_allowed(self):
        self.allowed(
            "developer",
            [
                "git status",
                "git diff origin/staging...HEAD",
                f"git checkout -b {KEY} FETCH_HEAD",
                "git add lib/x/file.py lib/x/tests/test_file.py",
                f"git -c user.name='Your Name' -c user.email='owner@example.com' commit -m \"{KEY} add the thing\"",
                'git commit -m "First line\n\nSecond paragraph, with a # and a ; in it"',
                "git rebase FETCH_HEAD",
                "cd app && uv run pytest ../../lib/x/tests -q",
                "uv run --package app ruff check lib/x/file.py",
                "uv sync --reinstall-package x-assets",
                "uv lock",
                "uv lock --check",
                "timeout 600 uv run pytest -q",
                "uv run pytest -q > /dev/null 2>&1",
                f"OFFICE_ROLE=developer python3 {CTL} push --ticket {KEY}",
                f"python3 {CTL} report --ticket {KEY} --status built --note 'tests pass'",
                f"python3 {CTL} run -- uv run scripts/smoke.py --run sales",
                f"python3 {CTL} run -- uv run --package app python {self.notes}/probe.py",
                "grep -rn 'foo' lib/x | head -20",
                "sed -i 's/\\r$//' lib/x/file.py",
                "ls -la; pwd",
                "az account show",
                "ping -c 1 db.internal.example",
                "grep -rn '^/usr/' lib",
                "echo see /etc/hosts for details",
                f"mkdir -p {self.notes}",
            ],
        )

    def test_pushing_and_history_rewrites_are_refused(self):
        self.refused(
            "developer",
            [
                f"git push origin {KEY}",
                "git push origin HEAD:staging",
                "git fetch origin staging",
                "git add -A",
                "git add .",
                "git commit --amend -m x",
                "git commit --no-verify -m x",
                "git checkout staging",
                "git checkout -b some-other-branch FETCH_HEAD",
                "git reset --hard HEAD~1",
                "git rebase -i HEAD~3",
                "git config user.name x",
                "git remote set-url origin https://example.com/x.git",
                "git -c core.sshCommand=evil status",
                "git -C /somewhere status",
                "git grep -O'sh -c id' foo",
                "git diff --output=/tmp/x",
                "git show HEAD:restricted/data/x.py",
                "GIT_ASKPASS=/tmp/x git status",
            ],
        )

    def test_only_the_developer_may_re_lock(self):
        self.refused("reviewer", ["uv lock"])
        self.refused("coauthor", ["uv lock"])

    def test_the_baseline_dependencies_and_other_roles_work_are_refused(self):
        self.refused(
            "developer",
            [
                "uv lock --upgrade",
                "uv lock --upgrade-package requests",
                "uv add requests",
                "uvx ruff check .",
                "pip install requests",
                "python3 -c 'print(1)'",
                f"python3 {CTL} thread-resolve --mr 1 --thread a --body x",
                f"OFFICE_ROLE=reviewer python3 {CTL} thread-resolve --mr 1 --thread a --body x",
                f"python3 {CTL} tick",
                f"python3 {CTL} report --ticket OTHER-9 --status built --note x",
                f"python3 {CTL} push --ticket=OTHER-9",
            ],
        )

    def test_protected_paths_secrets_and_the_outside_world_are_refused(self):
        self.refused(
            "developer",
            [
                "cat restricted/data/README.md",
                "grep -r token vendor/lib",
                "cat ~/.release-office/env",
                "echo $GITLAB_TOKEN",
                "cat .env",
                "cat ~/.ssh/id_rsa",
                "curl https://example.com",
                "wget https://example.com/x",
                "ssh somewhere",
                "claude -p 'do something'",
                "sudo ls",
                "cat /etc/passwd",
                "ls /home",
                "cd /path/to/your-repo",
                "sort --output=/etc/x lib/x/file.py",
                "cat lib/x/../../../../../etc/passwd",
            ],
        )

    def test_shell_tricks_are_refused(self):
        self.refused(
            "developer",
            [
                "echo $(whoami)",
                "echo `whoami`",
                "cat <<EOF\nhello\nEOF",
                "echo hi # a comment\nrm -rf lib",
                "rm -rf lib/x",
                "rm -r build",
                "find . -name '*.py' -delete",
                "find . -name '*.py' -exec rm {} ;",
                "echo x > /etc/passwd",
                "echo x > ../outside.txt",
                "sleep 5 &",
                "(cd lib && ls)",
                "git status; curl https://example.com",
                "awk 'BEGIN{system(\"id\")}'",
                "echo 'unbalanced",
            ],
        )
        self.assertFalse(self.bash("developer", "uv run pytest", run_in_background=True)[0])

    def test_files_it_may_and_may_not_touch(self):
        def path(relative):
            return str(self.tree / relative)

        for target in (path("lib/x/new.py"), str(self.notes / "brief.md")):
            self.assertTrue(self.ask("developer", "Write", file_path=target)[0], target)
        blocked = (
            path("restricted/data/x.py"),
            path(".git/config"),
            path(".claude/settings.local.json"),
            str(config.HOME / "guards" / "guard.py"),
            str(Path("~/.claude/settings.json").expanduser()),
            str(Path("~/.release-office/env").expanduser()),
            "/etc/hosts",
        )
        for target in blocked:
            self.assertFalse(self.ask("developer", "Edit", file_path=target)[0], target)
        for target in (path("lib/x/file.py"), "/tmp/tool-output.txt", str(config.HOME / "skills" / "office-developer" / "SKILL.md")):
            self.assertTrue(self.ask("developer", "Read", file_path=target)[0], target)
        for target in (path("restricted/data/x.py"), path(".env"), "/etc/passwd", str(Path("~/.release-office/env").expanduser())):
            self.assertFalse(self.ask("developer", "Read", file_path=target)[0], target)
        self.assertTrue(self.ask("developer", "Grep", pattern="restricted")[0])
        self.assertFalse(self.ask("developer", "Glob", pattern="/etc/**")[0])
        self.assertFalse(self.ask("developer", "Glob", pattern="../**/*.py")[0])


class Reviewer(Case):
    def test_it_reads_runs_and_posts_but_never_changes_code(self):
        self.allowed(
            "reviewer",
            [
                "git checkout --detach FETCH_HEAD",
                "git diff origin/staging...HEAD",
                "uv run pytest lib/x/tests -q",
                f"python3 {CTL} fetch --branch {KEY}",
                f"python3 {CTL} thread-post --mr 5 --file lib/x/file.py --line 3 --body-file {self.notes}/b1.md",
                f"python3 {CTL} thread-resolve --mr 5 --thread abc --body 'Verified at 1a2b3c4: lib/x/file.py:3'",
                f"python3 {CTL} report --ticket {KEY} --status passed --note 'all met'",
            ],
        )
        self.refused(
            "reviewer",
            [
                "git commit -m x",
                "git add lib/x/file.py",
                f"git checkout -b {KEY} FETCH_HEAD",
                "git stash",
                f"python3 {CTL} push --ticket {KEY}",
                f"python3 {CTL} mr-create --ticket {KEY} --title x --body-file y",
                "sed -i s/a/b/ lib/x/file.py",
                "touch lib/x/new.py",
                "echo x > lib/x/file.py",
            ],
        )
        self.assertFalse(self.ask("reviewer", "Edit", file_path=str(self.tree / "lib/x/file.py"))[0])
        self.assertTrue(self.ask("reviewer", "Write", file_path=str(self.notes / "requirement-map.md"))[0])


class Coauthor(Case):
    def test_it_reads_and_writes_its_findings_only(self):
        self.allowed(
            "coauthor",
            [
                "git log --no-merges origin/staging..HEAD --format='%h %s'",
                "uv run pytest lib/x/tests -q",
                f"python3 {CTL} report --ticket {KEY} --status clean",
            ],
        )
        self.refused("coauthor", ["git checkout --detach FETCH_HEAD", f"python3 {CTL} threads --mr 5", "git commit -m x"])
        self.assertTrue(self.ask("coauthor", "Write", file_path=str(self.notes / "coauthor-1.md"))[0])
        self.assertFalse(self.ask("coauthor", "Write", file_path=str(self.tree / "lib/x/file.py"))[0])


class Everyone(Case):
    def test_tools_that_reach_outside_are_refused(self):
        for tool in ("WebFetch", "WebSearch", "mcp__claude_ai_Gmail__authenticate", "AskUserQuestion", "SomeNewTool"):
            self.assertFalse(self.ask("developer", tool)[0], tool)
        for tool in ("Skill", "TodoWrite", "Agent"):
            self.assertTrue(self.ask("reviewer", tool)[0], tool)

    def test_a_session_without_a_role_or_ticket_gets_nothing(self):
        self.assertFalse(self.bash("", "ls")[0])
        self.assertFalse(self.bash("manager", "ls")[0])
        self.assertFalse(guard.decide("Bash", {"command": "ls"}, "developer", "", str(self.tree))[0])

    def test_the_guard_and_the_tool_agree_on_each_roles_commands(self):
        self.assertEqual(guard.OFFICE_COMMANDS, officectl.ALLOWED)


class Hook(Case):
    """The script as Claude Code runs it: JSON on stdin, a decision out."""

    def run_hook(self, payload, role="developer"):
        env = dict(os.environ, OFFICE_ROLE=role, OFFICE_TICKET=KEY, OFFICE_STATE_DIR=str(self.state), OFFICE_WORKTREE=str(self.tree))
        script = str(config.HOME / "guards" / "guard.py")
        return subprocess.run([sys.executable, script], input=payload, env=env, capture_output=True, text=True, check=False)

    def call(self, command):
        return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.tree)})

    def test_an_allowed_call_exits_zero_with_an_allow_decision(self):
        done = self.run_hook(self.call("git status"))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_a_refused_call_exits_two_and_says_why(self):
        done = self.run_hook(self.call("git push origin staging"))
        self.assertEqual(done.returncode, 2)
        self.assertIn("Refused by the Release Office guard", done.stderr)
        self.assertIn("officectl push", done.stderr)

    def test_a_broken_call_is_refused_not_waved_through(self):
        self.assertEqual(self.run_hook("this is not json").returncode, 2)

    def test_every_decision_is_logged(self):
        self.run_hook(self.call("git status"))
        self.run_hook(self.call("curl https://example.com"))
        lines = [json.loads(line) for line in (self.state / "logs" / "guard.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["allowed"] for line in lines], [True, False])


if __name__ == "__main__":
    unittest.main()
