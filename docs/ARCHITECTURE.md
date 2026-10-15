# Release Office — architecture

## Shape

Everything runs on the owner's machine. A browser page cannot start an agent or hold a
token, so there is a small local backend.

```
control page (ui/index.html)  ──HTTP──▶  server.py  ──▶  the manager (office/manager.py)
                                             │                 │
                                             │                 ├─ Jira and GitLab clients
                                             │                 └─ the task list (~/.release-office/office.json)
                                             └─ starts each role's session through the Claude CLI (auto mode)

each role's session ──▶ officectl.py ──▶ reads its message, the ticket, GitLab; reports to the manager
```

| Part | Where | What it does |
|---|---|---|
| Backend | `server.py` | Serves the control page, runs the manager, and in auto mode starts each role's session |
| Manager | `office/manager.py` | A state machine. On each pass it reads the reports, decides the next message and writes to Jira and GitLab |
| Role tool | `officectl.py` | What an agent runs: its message, the ticket, push, open the merge request, list and answer threads, report |
| Task list | `~/.release-office/office.json` | Tickets, their state, pending messages, reports and the message log, written under a file lock |
| Plan check | `office/plan.py` | The ticket's required shape, and the check that says whether a ticket is ready |
| Guard | `guards/guard.py` | The hook every unattended session runs behind. See [`GUARDRAILS.md`](GUARDRAILS.md) |
| Clients | `office/jira.py`, `office/gitlab.py` | Standard library only |
| Demo | `office/demo.py` | A pretend Jira, GitLab and set of agents, for the demo mode and the tests |

## Modes

| Mode | Jira and GitLab | Role sessions | The manager moves |
|---|---|---|---|
| `demo` | Pretend | Scripted | On its own, every second |
| `manual` | Real | The owner starts each skill | Only when the owner presses **Run one step**, after a preview |
| `auto` | Real | The backend starts each one through the Claude CLI, behind the guard | On its own once the owner has pressed **Start** |

## The ticket

Everything downstream works from the Jira ticket, so its shape is fixed. The
`/office-planner` skill writes it from the owner's planning conversation.

| Section | Who uses it |
|---|---|
| Goal, Problem, Solution | Everyone, for the why |
| Requirements, `R1`, `R2`, each ending in `Check:` | The developer builds them. The co-author's and the reviewer's maps have one row for each |
| Decisions, `D1`, `D2` | The developer follows them. The co-author and the reviewer check them |
| In scope, Out of scope | The test for drift |
| Change type, with `Expected difference:` for an output change | What the result is compared to |
| How to verify, Docs to update, Risks | The developer's test plan and doc sweep |
| Facts checked, `F1`, `F2`, each ending in `Source:` | What the plan took to be true, and where it was looked up |
| Open questions | Must read "None." |
| For the office | The repo, and what the ticket depends on |

`officectl ticket-check` lists what is wrong with a ticket: a missing, repeated or
misplaced section, a fact without a source, a requirement without a check, a missing test
command, unsettled wording ("TBD", "probably", "assume") or a question in a requirement.
The manager runs the same check before it starts a ticket, and turns a failing one away
with the list on the page. `ticket-check`, `ticket-create` and `ticket-update` also look up
every path the ticket names on the target branch, and refuse one that is not there, one
marked `(new)` that already exists, and a protected path in scope.

## The hub

No agent messages another. A role ends its work with one `officectl report`. The manager
reads the report on its next pass and writes the next message, which the receiving role
reads with `officectl inbox`.

| Report | From | The manager then |
|---|---|---|
| `built` | Developer | Asks the co-author to read the local change |
| `clean`, `findings` | Co-author | Tells the developer to open the merge request, or to fix the findings. `findings --drift` after the last round stops the ticket at the owner |
| `mr_open` | Developer | Checks the merge request is the ticket's branch into the target branch, moves Jira to Completed, writes the link there, asks the reviewer |
| `threads_posted` | Reviewer | Tells the developer there are threads to fix |
| `fixed` | Developer | Asks the reviewer to check the fixes, or returns the merge request to the person who commented |
| `passed` | Reviewer | Waits for the checks: pipeline green on the head commit, not a draft, every thread resolved, no conflicts. Then moves Jira to Reviewed and assigns the maintainer |
| `blocked` | Developer | Shows the question to the owner under "Needs you", with everything the developer recorded |
| `failed` | Any role | Stops the ticket and tells the owner why |

The office writes two things on a Jira ticket: the merge request link at Completed, and an
answer of the owner's that he marks as a core design decision. Every answer is also kept in
the ticket's notes, where `officectl ticket` prints it for the developer, the co-author and
the reviewer.

Settings that change the flow: `agent_review.enabled` (on) puts the reviewer agent in it;
`human_review.enabled` (off) adds a named person's approval before the merge;
`limits` caps the co-author rounds (2), the review rounds (3) and the tickets in the agents'
hands at once (1).

## Ticket states

```
listed ─▶ working ─▶ co_review ─▶ opening_mr ─▶ in_review ⇄ fixing
                         ▲  │                       │
                         └──┘ findings              ▼ passed
                                             waiting for checks ─▶ waiting_merge ─▶ on_staging
blocked / needs_owner / needs_plan: waiting on the owner
```

## Automatic mode

`ClaudeLauncher` (`office/sessions.py`) runs `claude -p` with the role's skill as its
instructions, in the ticket's own working copy, with the guard's settings and
`--permission-mode dontAsk`, and `OFFICE_ROLE`, `OFFICE_TICKET` and `OFFICE_WORKTREE` set.
It refuses to start a session without a working copy or without the guard, and passes no
credential in the session's environment. The developer's session is resumed by its id for
every later message, so it keeps the whole ticket's context; the co-author and the reviewer
start fresh. A session ends by reporting. The backend sees the task list change and runs the
next pass at once. A session that ends without a report, or runs past `claude.max_minutes`,
stops the ticket at the owner.

## Guardrails in the code

| Guardrail | Where |
|---|---|
| The office writes only on merge requests it opened, and an unattended session only on its own ticket's | `officectl.py`, `own_mr` |
| A merge request title with a ticket number, or a description with a session link, is refused | `officectl.py`, `mr-create` |
| Pushing to a protected branch, or over a diverged remote, is refused | `office/gitops.py`, `push` |
| A branch or an open merge request the office did not create stops the ticket | `office/manager.py`, `_listed` |
| A green pipeline counts only on the head commit | `office/facts.py`, `mr_facts` |
| A report is kept for the next pass if Jira or GitLab was unreachable | `office/manager.py`, `_step` |
| A restarted backend comes up closed | `server.py`, `_reset` |
| The control page accepts requests from the local machine only, with a custom header | `server.py`, `Handler` |
