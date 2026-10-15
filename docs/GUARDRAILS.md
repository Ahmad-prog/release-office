# Release Office — guardrails for unattended sessions

A session nobody is watching cannot ask for approval. So it runs with two layers.

1. **A permission mode that denies whatever is not allowed** (`dontAsk`). Nothing runs by
   default.
2. **A guard that Claude Code calls before every tool use** (`guards/guard.py`). It looks at
   the call and the session's role, and allows it or refuses it with a reason.

Both come from `guards/session-settings.json`, which the backend passes to every session it
starts. The session also needs `OFFICE_ROLE`, `OFFICE_TICKET` and `OFFICE_WORKTREE` in its
environment. Without a role and a ticket the guard refuses everything, and if the guard
itself fails, nothing is allowed.

Every decision is written to `~/.release-office/logs/guard.jsonl`, so you can see afterwards
what a session tried.

A refusal is not meant to stop a ticket. The skills tell each role not to look for another
way to do a refused thing, to finish the rest of its work, and to say in its report what was
refused.

## What each role may do

| | Developer | Co-author | Reviewer |
|---|---|---|---|
| Read files | Its working copy, its notes folder, the office's skills and docs, `/tmp` | The same | The same |
| Write files | Its working copy and its notes folder | Its notes folder only | Its notes folder only |
| git | Read commands. Create and stay on the ticket's branch. Add named files, commit, restore, stash, rebase onto `FETCH_HEAD` | Read commands only | Read commands, and check out the merge request's head |
| Run code | The commands in `guard.run_commands`, `uv lock` | The commands in `guard.run_commands` | The commands in `guard.run_commands` |
| Office tool | Push, open the merge request, reply, comment, report | Read its message and the ticket, report | Post, reply, resolve its own threads, comment, report |

All three may also use plain read-only commands (`ls`, `cat`, `grep`, `find`, `diff` and
the like). `guard.run_commands` in `office.config.json` lists the commands that run your
repo's code and tests; it is `uv run` and `uv sync` unless you change it.

## Always refused

- **Protected paths.** Anything under the folders in `repo.protected_paths`.
- **Credentials.** The office's env file, `.env` files, SSH and cloud credential folders,
  and any command that names a variable holding a token, a password, a secret or a key.
- **Pushing by hand.** `git push`, `fetch`, `pull`, `remote`, `config`, `-c` options other
  than the commit identity, `--amend`, `--no-verify`, hard resets, interactive rebases,
  `git add -A`, and switching to any branch but the ticket's own. Pushing goes through
  `officectl push`, which holds the token.
- **Dependencies.** `uv add`, `uvx`, `pip`, and `uv lock` with an upgrade option.
- **Another role's work, or another ticket.** A developer cannot resolve a thread. No role
  can name a different role or a different ticket.
- **The outside world.** `curl`, `wget`, `ssh`, web fetch and search, every connector, and
  starting another Claude session.
- **Its own guardrails.** Anything in the office folder, Claude Code's settings, and `.git`
  or `.claude` inside the working copy.
- **Shell tricks.** Command substitution, here-documents, subshells, background jobs,
  recursive deletes, `find` with an action, and redirection to a file outside the folders
  the role may write.
- **Asking the user.** Nobody is there. The session reports to the manager instead.

## What it does not do

It stops accidents and the direct routes. It is not a sandbox.

- A role that may run the tests can run code, and code can do what the guard would refuse.
- The network is not restricted for code run through the allowed run commands.
- A broad search such as `grep -r` over the whole working copy also reads protected folders
  inside it.

The walls behind the guard are the review, the protected branches, and the merge by a
person. A stronger wall in front of it would be Claude Code's own sandbox for shell
commands, with a list of allowed folders and hosts.
