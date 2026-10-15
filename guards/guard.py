#!/usr/bin/env python3
"""The guard every unattended Release Office session runs behind.

Claude Code calls this before each tool use (a PreToolUse hook) and passes the call as JSON
on stdin. The guard allows what the session's role needs and refuses the rest. The session
runs in the permission mode that denies whatever is not allowed, so if this script fails,
nothing runs.

It stops accidents and the direct routes. It is not a sandbox: a role that may run the
tests can run code. The outer walls are the review, the protected branches and the merge
by a person.
"""

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
OFFICECTL = (HOME / "officectl.py").resolve()

# Mirrors ALLOWED in officectl.py. A test keeps the two in step.
OFFICE_COMMANDS = {
    "developer": {"status", "inbox", "ticket", "ticket-check", "report", "mr", "run", "fetch", "push", "mr-create", "threads", "thread-reply", "comment"},
    "coauthor": {"status", "inbox", "ticket", "ticket-check", "report"},
    "reviewer": {"status", "inbox", "ticket", "ticket-check", "report", "mr", "run", "fetch", "threads", "thread-post", "thread-reply", "thread-resolve", "comment"},
}

SECRET_MARKS = (".release-office/env", "git-askpass", ".git-credentials", ".netrc", "/.ssh", "/.azure", "/.aws", "/.gnupg")
# Any variable whose name says it holds a secret.
SECRET_VARIABLE = re.compile(r"\b[A-Z0-9_]*(TOKEN|PASSWORD|SECRET|API_KEY|PRIVATE_KEY)[A-Z0-9_]*\b")
SHELL_TRICKS = ("$(", "`", "<(", ">(", "<<")
SAFE_ASSIGN = {"OFFICE_DRY", "TZ", "LANG", "LC_ALL", "NO_COLOR", "PYTHONUNBUFFERED"}

READ_TOOLS = {"Read": "file_path", "NotebookRead": "notebook_path", "LS": "path", "Glob": "path", "Grep": "path"}
WRITE_TOOLS = {"Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
HARMLESS_TOOLS = {"Skill", "TodoWrite", "ToolSearch", "Agent", "Task", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "BashOutput", "KillShell", "KillBash"}

PLAIN = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find", "sort", "uniq", "cut", "diff", "comm",
    "paste", "echo", "printf", "pwd", "test", "[", "true", "false", "stat", "file", "basename", "dirname", "date", "tr",
    "jq", "nl", "column", "tac", "realpath", "which", "sleep", "awk", "sed", "ping", "tree", "du", "md5sum", "sha256sum",
}
# The first plain argument of these is a pattern or a script, not a file.
PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "sed", "awk", "jq"}
NO_FILES = {"echo", "printf", "true", "false", "sleep", "date", "pwd", "which", "test", "["}
FILE_WRITERS = {"mkdir", "touch", "cp", "mv", "rm", "tee"}
FIND_ACTIONS = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}

GIT_READ = {
    "status", "diff", "log", "show", "rev-parse", "merge-base", "ls-files", "blame", "grep", "describe", "shortlog",
    "cat-file", "ls-tree", "rev-list", "show-ref", "for-each-ref", "name-rev",
}
GIT_BRANCH_LIST = {"-a", "-r", "-v", "-vv", "-l", "--all", "--remotes", "--list", "--show-current"}
GIT_DEVELOPER = {"add", "commit", "restore", "rm", "mv", "stash", "apply", "cherry-pick", "rebase", "reset"}
GIT_HINTS = {
    "push": "Push with `officectl push --ticket <KEY>`.",
    "fetch": "Fetch with `officectl fetch --branch <name>`.",
    "pull": "Fetch with `officectl fetch`, then rebase onto FETCH_HEAD.",
}


def load_settings():
    """The repo's protected paths and the commands that run its code, from office.config.json."""
    found = {"protected_paths": [], "run_commands": ["uv run", "uv sync"]}
    try:
        cfg = json.loads((HOME / "office.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return found
    found["protected_paths"] = list((cfg.get("repo") or {}).get("protected_paths") or [])
    found["run_commands"] = list((cfg.get("guard") or {}).get("run_commands") or found["run_commands"])
    return found


SETTINGS = load_settings()


PROTECTED_PATH = "That path is protected: the office never reads or changes it."


class Refuse(Exception):
    """A call the session's role may not make. The message is shown to the agent."""


class Ctx:
    """Who is asking, and which folders that role may read and write."""

    def __init__(self, role, ticket, cwd, worktree=None, state=None):
        self.role, self.ticket = role, ticket
        self.cwd = Path(cwd).resolve()
        self.worktree = Path(worktree).resolve() if worktree else self.cwd
        state = Path(state or os.environ.get("OFFICE_STATE_DIR") or "~/.release-office").expanduser().resolve()
        self.notes = state / "tickets" / ticket
        skills = Path("~/.claude/skills").expanduser().resolve()
        self.readable = [self.worktree, self.notes, HOME / "skills", HOME / "docs", skills, Path("/tmp").resolve()]
        self.writable = [self.notes] + ([self.worktree] if role == "developer" else [])
        # Paths the office never touches: the repo's own list, from office.config.json.
        self.restricted = [self.worktree / part for part in SETTINGS["protected_paths"]]
        # Never written by hand: git's own files and Claude Code settings.
        self.protected = [self.worktree / part for part in (".git", ".claude")]


def inside(path, roots):
    return any(path == root or root in path.parents for root in roots)


def where(arg, ctx):
    """The folder or file an argument points at, or None when it cannot be known."""
    if "$" in arg or "`" in arg:
        return None
    path = Path(arg).expanduser()
    return (path if path.is_absolute() else ctx.cwd / path).resolve()


def looks_like_path(arg):
    return arg.startswith(("/", "~", "./", "../")) or "/" in arg


def secret_in(text):
    if any(mark in text for mark in SECRET_MARKS) or SECRET_VARIABLE.search(text):
        return True
    return bool(re.search(r"(^|[/\s'\"=])\.env(\.[\w.-]+)?($|[\s'\";])", text))


def check_read(path, ctx):
    if path is None:
        raise Refuse("That path cannot be checked. Name it plainly, without variables.")
    if inside(path, ctx.restricted):
        raise Refuse(PROTECTED_PATH)
    if secret_in(str(path)):
        raise Refuse("Credentials are not yours to read. The office tool reads them itself.")
    if not inside(path, ctx.readable):
        raise Refuse("Read only inside your working copy and your notes folder.")


def check_write(path, ctx):
    if path is None:
        raise Refuse("That path cannot be checked. Name it plainly, without variables.")
    if inside(path, ctx.restricted):
        raise Refuse(PROTECTED_PATH)
    if secret_in(str(path)):
        raise Refuse("Credentials are not yours to write.")
    if not inside(path, ctx.writable):
        where_to = "your working copy and your notes folder" if ctx.role == "developer" else "your notes folder"
        raise Refuse(f"The {ctx.role} writes only inside {where_to}.")
    if inside(path, ctx.protected):
        raise Refuse("That folder is never edited by hand: git's own files and Claude Code settings.")


# ---------------------------------------------------------------------- shell commands


def flatten_lines(command):
    """Turn line breaks outside quotes into `;`, so each line is checked as its own command."""
    out, quote, index = [], "", 0
    while index < len(command):
        char = command[index]
        pair = command[index : index + 2]
        if quote:
            if char == "\\" and quote == '"' and len(pair) == 2:
                out.append(pair)
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "\\" and len(pair) == 2:
            out.append(" " if pair[1] == "\n" else pair)
            index += 2
            continue
        elif char == "\n":
            out.append(" ; ")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def split_commands(command, ctx):
    """The simple commands in a line. Redirections are checked here and dropped."""
    lexer = shlex.shlex(flatten_lines(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise Refuse(f"The command could not be read ({exc}).") from exc
    commands, current, index = [], [], 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("&&", "||", ";", "|"):
            commands.append(current)
            current = []
        elif token in (">", ">>", "<", ">&", "<&", "&>", "&>>"):
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            check_redirect(token, target, ctx)
            if current and current[-1].isdigit():
                current.pop()
            index += 1
        elif token and set(token) <= set("();<>|&"):
            raise Refuse(f"`{token}` is not allowed. Run one plain command at a time, in the foreground.")
        else:
            current.append(token)
        index += 1
    commands.append(current)
    return [cmd for cmd in commands if cmd]


def check_redirect(op, target, ctx):
    if target == "/dev/null" or (op in (">&", "<&") and (target.isdigit() or target == "-")):
        return
    if op == "<":
        check_read(where(target, ctx), ctx)
    else:
        check_write(where(target, ctx), ctx)


def check_bash(args, ctx):
    command = args.get("command") or ""
    if args.get("run_in_background"):
        raise Refuse("Nothing runs in the background in an unattended session.")
    if not command.strip():
        raise Refuse("The command is empty.")
    for trick in SHELL_TRICKS:
        if trick in command:
            raise Refuse(f"`{trick}` is not allowed. Put the text in a file in your notes folder and pass the file.")
    if secret_in(command):
        raise Refuse("Credentials are not yours to read or print. The office tool reads them itself.")
    for argv in split_commands(command, ctx):
        check_simple(argv, ctx)


def check_simple(argv, ctx):
    argv = list(argv)
    while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
        name, value = argv.pop(0).split("=", 1)
        if name == "OFFICE_ROLE":
            if value != ctx.role:
                raise Refuse(f"You are the {ctx.role}. You cannot act as the {value}.")
        elif name not in SAFE_ASSIGN:
            raise Refuse(f"Setting `{name}` for a command is not allowed.")
    if not argv:
        return
    head, rest = argv[0], argv[1:]
    for arg in rest:
        if not arg.startswith("-") and (path := where(arg, ctx)) and inside(path, ctx.restricted):
            raise Refuse(PROTECTED_PATH)
    if head == "time":
        return check_simple(rest, ctx)
    if head == "timeout":
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return check_simple(rest[1:], ctx)
    if head == "cd":
        target = where(rest[0], ctx) if rest else None
        if target is None or not inside(target, [ctx.worktree, ctx.notes]):
            raise Refuse("Stay inside your working copy and your notes folder.")
        return None
    if head in PLAIN:
        return check_plain(head, rest, ctx)
    if head in FILE_WRITERS:
        return check_file_writer(head, rest, ctx)
    if head == "git":
        return check_git(rest, ctx)
    if head == "uv":
        return check_uv(rest, ctx)
    if head in ("python3", "python"):
        return check_python(rest, ctx)
    if head == "az" and rest[:2] == ["account", "show"]:
        return None
    # The commands that run the repo's code and tests, as office.config.json lists them.
    if any(" ".join(argv).startswith(prefix) for prefix in SETTINGS["run_commands"]):
        return None
    raise Refuse(f"`{head}` is not a command the {ctx.role} runs unattended.")


def check_plain(head, rest, ctx):
    if head == "find" and FIND_ACTIONS & set(rest):
        raise Refuse("`find` may only list. It may not run or delete anything.")
    if head == "awk" and any("system(" in arg for arg in rest):
        raise Refuse("`awk` may not run other commands.")
    if head == "ping" and "-c" not in rest:
        raise Refuse("Give `ping` a count with -c.")
    in_place = head == "sed" and any(arg == "--in-place" or re.fullmatch(r"-[a-zA-Z]*i.*", arg) for arg in rest)
    plain = [arg for arg in rest if not arg.startswith("-")]
    if in_place:
        for arg in plain[1:]:
            check_write(where(arg, ctx), ctx)
        return
    if head in NO_FILES:
        return
    if head in PATTERN_FIRST:
        plain = plain[1:]
    # A path can also hide in an option, as in --output=/somewhere.
    values = [arg.split("=", 1)[1] for arg in rest if arg.startswith("--") and "=" in arg]
    for arg in plain + values:
        if arg != "/dev/null" and looks_like_path(arg):
            check_read(where(arg, ctx), ctx)


def check_file_writer(head, rest, ctx):
    if head == "rm" and any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", arg) or arg == "--recursive" for arg in rest):
        raise Refuse("No recursive deletes. Remove tracked files with `git rm`.")
    paths = [arg for arg in rest if not arg.startswith("-")]
    if not paths:
        raise Refuse(f"`{head}` needs a path.")
    sources = paths[:-1] if head == "cp" else []
    for arg in sources:
        check_read(where(arg, ctx), ctx)
    for arg in paths[len(sources) :]:
        check_write(where(arg, ctx), ctx)


def check_git(args, ctx):
    args = list(args)
    while args and args[0].startswith("-"):
        if args[0] == "-c" and len(args) > 1 and args[1].split("=", 1)[0] in ("user.name", "user.email"):
            args = args[2:]
        elif args[0] in ("--no-pager", "-P"):
            args = args[1:]
        else:
            raise Refuse(f"`git {args[0]}` is not allowed. Run git plainly, inside your working copy.")
    if not args:
        return
    sub, rest = args[0], args[1:]
    for arg in rest:
        if arg.startswith(("--output", "--open-files-in-pager", "-O")):
            raise Refuse(f"`git {sub} {arg.split('=')[0]}` is not allowed: it can write files or run a program.")
        if any(part.rstrip("/") in arg for part in SETTINGS["protected_paths"]):
            raise Refuse(PROTECTED_PATH)
    if sub in GIT_READ:
        return
    if sub == "branch" and set(rest) <= GIT_BRANCH_LIST:
        return
    detached = len(rest) == 2 and rest[0] == "--detach" and (rest[1] == "FETCH_HEAD" or re.fullmatch(r"[0-9a-f]{7,40}", rest[1]))
    if sub in ("checkout", "switch") and detached and ctx.role in ("developer", "reviewer"):
        return
    if ctx.role != "developer":
        raise Refuse(f"The {ctx.role} does not change the repository (`git {sub}`).")
    if sub in ("checkout", "switch"):
        new = "-b" if sub == "checkout" else "-c"
        start = len(rest) == 3 and rest[:2] == [new, ctx.ticket] and (rest[2] == "FETCH_HEAD" or rest[2].startswith("origin/"))
        if start or rest == [ctx.ticket] or (sub == "checkout" and rest[:1] == ["--"]):
            return
        raise Refuse(f"Stay on your ticket's branch. Create it with `git checkout -b {ctx.ticket} FETCH_HEAD`.")
    if sub not in GIT_DEVELOPER:
        hint = GIT_HINTS.get(sub, "")
        raise Refuse(f"`git {sub}` is not allowed. {hint}".strip())
    if sub == "add" and {"-A", "--all", ".", "-u", "--update", "*"} & set(rest):
        raise Refuse("Stage the files you changed by name, never everything at once.")
    if sub == "commit" and any(arg.startswith("--amend") or arg in ("--no-verify", "-n") for arg in rest):
        raise Refuse("Make a new commit. Do not amend, and do not skip the hooks.")
    if sub == "rebase":
        shapes = (["--continue"], ["--abort"], ["--skip"], ["FETCH_HEAD"])
        if rest not in shapes and not (len(rest) == 1 and rest[0].startswith("origin/")):
            raise Refuse("Rebase only onto FETCH_HEAD or origin/<branch>, and never interactively.")
    if sub == "reset" and "--hard" in rest:
        raise Refuse("No hard resets. Use `git rebase --abort` or `git restore`.")


def check_uv(args, ctx):
    plain = [arg for arg in args if not arg.startswith("-")]
    # The developer may re-lock after a dependency change the owner agreed to. Plainly: no upgrades.
    if ctx.role == "developer" and args in (["lock"], ["lock", "--check"]):
        return
    if not plain or plain[0] not in ("run", "sync"):
        tried = plain[0] if plain else "uv"
        allowed = "`uv run`, `uv sync` and a plain `uv lock`" if ctx.role == "developer" else "`uv run` and `uv sync` only"
        raise Refuse(f"`uv {tried}` is not allowed. Adding a dependency is the owner's call. Use {allowed}.")


def check_python(args, ctx):
    if len(args) < 2 or Path(args[0]).resolve() != OFFICECTL:
        raise Refuse("The only bare python3 call is the office tool. Run project code with `uv run`.")
    sub = args[1]
    if sub not in OFFICE_COMMANDS.get(ctx.role, ()):
        raise Refuse(f"The {ctx.role} does not run `officectl {sub}`. That belongs to another role.")
    named = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--ticket"]
    named += [arg.split("=", 1)[1] for arg in args if arg.startswith("--ticket=")]
    if any(key != ctx.ticket for key in named):
        raise Refuse(f"This session works on {ctx.ticket} and no other ticket.")
    if sub == "run":
        inner = args[2:]
        check_simple(inner[1:] if inner[:1] == ["--"] else inner, ctx)


# ---------------------------------------------------------------------- the decision


def decide(tool, args, role, ticket, cwd, worktree=None, state=None):
    """Returns ``(allowed, reason)`` for one tool call."""
    if role not in OFFICE_COMMANDS or not ticket:
        return False, "This session has no office role and ticket, so nothing is allowed."
    ctx = Ctx(role, ticket, cwd, worktree, state)
    try:
        if tool == "Bash":
            check_bash(args, ctx)
        elif tool in READ_TOOLS:
            target = args.get(READ_TOOLS[tool])
            check_read(where(target, ctx) if target else ctx.cwd, ctx)
            pattern = str(args.get("pattern") or "")
            if tool == "Glob" and ".." in pattern:
                raise Refuse("Search inside your working copy, not above it.")
            if tool == "Glob" and pattern.startswith(("/", "~")):
                check_read(where(re.split(r"[*?\[{]", pattern)[0], ctx), ctx)
        elif tool in WRITE_TOOLS:
            target = args.get(WRITE_TOOLS[tool])
            check_write(where(target, ctx) if target else None, ctx)
        elif tool in HARMLESS_TOOLS:
            pass
        elif tool == "AskUserQuestion":
            raise Refuse("Nobody is here to answer. Report `blocked` with your question instead.")
        elif tool in ("WebFetch", "WebSearch") or tool.startswith("mcp__"):
            raise Refuse("An unattended session does not reach outside services.")
        else:
            raise Refuse(f"`{tool}` is not a tool the {role} uses unattended.")
    except Refuse as exc:
        return False, str(exc)
    return True, "allowed"


def log(call, role, ticket, allowed, reason):
    """Every decision is kept, so the owner can see afterwards what a session tried."""
    try:
        folder = Path(os.environ.get("OFFICE_STATE_DIR") or "~/.release-office").expanduser() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        args = call.get("tool_input") or {}
        what = args.get("command") or args.get("file_path") or args.get("path") or args.get("pattern") or ""
        entry = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "role": role,
            "ticket": ticket,
            "tool": call.get("tool_name"),
            "what": str(what)[:300],
            "allowed": allowed,
            "reason": "" if allowed else reason,
        }
        with open(folder / "guard.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def main():
    role, ticket = os.environ.get("OFFICE_ROLE", ""), os.environ.get("OFFICE_TICKET", "")
    call = {}
    try:
        call = json.load(sys.stdin)
        cwd = call.get("cwd") or os.getcwd()
        worktree = os.environ.get("OFFICE_WORKTREE")
        allowed, reason = decide(call.get("tool_name") or "", call.get("tool_input") or {}, role, ticket, cwd, worktree)
    except Exception as exc:  # a broken guard must refuse, never wave things through
        allowed, reason = False, f"The office guard failed ({exc}), so nothing is allowed."
    log(call if isinstance(call, dict) else {}, role, ticket, allowed, reason)
    if allowed:
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": "Release Office guard"}
        print(json.dumps({"hookSpecificOutput": decision}))
        sys.exit(0)
    status = "blocked" if role == "developer" else "failed"
    advice = f"Do not look for another way to do this. Report `{status}` to the manager and quote the refused call."
    print(f"Refused by the Release Office guard: {reason} {advice}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
