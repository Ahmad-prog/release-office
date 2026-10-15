"""Office configuration: one JSON file, with tokens taken from the environment."""

import copy
import json
import os
from pathlib import Path

HOME = Path(os.environ.get("OFFICE_HOME") or Path(__file__).resolve().parent.parent)
# Skills and the guard settings may name the office folder by this placeholder. It is replaced with HOME when used.
PLACEHOLDER = "/path/to/release-office"

DEFAULTS = {
    "owner": {
        "name": "The owner",
        "gitlab_username": "owner",
        "git_name": "Your Name",
        "git_email": "owner@example.com",
    },
    "jira": {
        "base_url": "https://your-company.atlassian.net",
        "project": "PROJ",
        "board_id": 1,
        "sprint": "Sprint 1",
        "sprint_field": "customfield_10020",
        # A ticket is taken only when its description passes the plan check (office/plan.py).
        "require_plan": True,
        # all: every In Progress ticket of the owner in the sprint, including one already there at Start.
        # after_start: only tickets moved to In Progress after Start. manual_only: only tickets added by key.
        "pickup": "all",
        "status": {
            "in_progress": "In Progress",
            "completed": "Completed",
            "reviewed": "Reviewed",
            "deployed": "Deployed to Staging",
        },
    },
    "gitlab": {"base_url": "https://gitlab.example.com", "project_id": 0, "target_branch": "staging"},
    "repo": {
        "path": "/path/to/your-repo",
        "worktrees": "~/.release-office/worktrees",
        # Paths the agents never read or change, such as regulated data or vendored code.
        "protected_paths": [],
    },
    # The commands the guard lets a session use to run the repo's code and tests.
    "guard": {"run_commands": ["uv run", "uv sync"]},
    "people": {
        # The GitLab account of whoever does the human review. Set it in office.config.json.
        "human_reviewer": "",
        "merger": "maintainer",
        "merger_name": "the maintainer",
    },
    # The human reviewer's approval is what sends a merge request on for merge.
    "human_review": {"enabled": False},
    # Off for now (2026-10-10): no reviewer agent. The merge request goes from the developer and
    # the co-author straight to the human reviewer. On: the reviewer agent reviews first.
    "agent_review": {"enabled": True},
    "limits": {"coauthor_loops": 2, "review_rounds": 3, "in_flight": 1},
    # handed_to_person: the next ticket starts once a merge request is with a person.
    # waiting_merge: it starts only once the merge request is assigned for merge.
    "start_next_when": "handed_to_person",
    # Auto mode's sessions. max_minutes stops one that runs too long; 0 means no limit.
    "claude": {"bin": "claude", "model": "", "max_budget_usd": 0, "max_minutes": 180, "extra_args": []},
    "poll_seconds": 60,
    "state_dir": "~/.release-office",
    "port": 8765,
}

ROLES = ("developer", "coauthor", "reviewer", "manager")
HUMAN_REVIEWER = "the human reviewer"


def _merge(base, over):
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = Path(path or os.environ.get("OFFICE_CONFIG") or HOME / "office.config.json")
    if path.exists():
        _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
    return cfg


def shown(cfg, who):
    """How the office names an account. The human reviewer goes by the role, never by a name."""
    people = cfg["people"]
    if who and who in (people["human_reviewer"], "human_reviewer"):
        return HUMAN_REVIEWER
    if who and who == people["merger"]:
        return people["merger_name"]
    if who and who == cfg["owner"]["gitlab_username"]:
        return cfg["owner"]["name"]
    return who


def state_dir(cfg):
    return Path(os.environ.get("OFFICE_STATE_DIR") or cfg["state_dir"]).expanduser()


def ticket_dir(cfg, key):
    """Where a ticket's brief, findings and notes live. Outside any repo, so never committed."""
    path = state_dir(cfg) / "tickets" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_file():
    """Where the owner may keep the tokens: outside the project, one NAME=value per line."""
    home = os.environ.get("OFFICE_STATE_DIR") or DEFAULTS["state_dir"]
    return Path(os.environ.get("OFFICE_ENV_FILE") or Path(home).expanduser() / "env")


def stored_env():
    """Every NAME=value pair in the owner's env file."""
    path, values = env_file(), {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ").strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip("'\"")
    return values


def tokens():
    """The environment wins. Anything missing there is read from the owner's env file."""
    stored = stored_env()

    def read(name):
        return os.environ.get(name) or stored.get(name, "")

    return {"jira_email": read("JIRA_EMAIL"), "jira_token": read("JIRA_TOKEN"), "gitlab_token": read("GITLAB_TOKEN")}


def missing_tokens():
    names = {"jira_email": "JIRA_EMAIL", "jira_token": "JIRA_TOKEN", "gitlab_token": "GITLAB_TOKEN"}
    return [names[key] for key, value in tokens().items() if not value]


def skill_text(role):
    """The role's runbook, without its YAML front matter."""
    text = (HOME / "skills" / f"office-{role}" / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.replace(PLACEHOLDER, str(HOME)).strip()
