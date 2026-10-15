"""Everything the owner needs to settle a ticket that is waiting on him.

The manager gathers it from what the developer has already recorded: its question and the
note it wrote about it, its brief, the ticket's own requirements, and every message that
passed through the manager for this ticket.
"""

from pathlib import Path

from office import plan
from office.api import ApiError
from office.config import shown, ticket_dir
from office.manager import LABEL

MAX_FILE = 20000
ON_THE_TICKET = ("Goal", "Requirements", "Decisions", "In scope", "Out of scope")


def read_note(folder, name):
    """A file from the ticket's notes folder, and only from there."""
    if not name:
        return ""
    folder = Path(folder).resolve()
    path = Path(name)
    path = (path if path.is_absolute() else folder / path).resolve()
    if folder not in path.parents or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:MAX_FILE] + ("\n[cut short]" if len(text) > MAX_FILE else "")


def kind_of(ticket):
    return {"blocked": "question", "needs_plan": "ticket", "needs_owner": "stopped"}.get(ticket["state"], "")


def build(cfg, state, key, jira=None):
    """The context for one waiting ticket, as titled pieces of text in reading order."""
    ticket = state["tickets"].get(key)
    if not ticket:
        return []
    folder = ticket_dir(cfg, key)
    parts = []

    def add(title, text):
        if text and text.strip():
            parts.append({"title": title, "text": text.strip()})

    question = ticket.get("question") or {}
    if ticket["state"] == "blocked":
        add("The developer's question", question.get("text", ""))
        add("What the developer wrote about it", read_note(folder, question.get("file") or "question.md"))
    elif ticket["state"] == "needs_plan":
        add("What must be fixed on the ticket", "\n".join(f"- {fault}" for fault in ticket.get("plan_problems") or []))
    else:
        add("Why the office stopped", ticket.get("error", ""))

    said = [f"{item['from'].capitalize()}: {item['text']}" for item in ticket.get("exchange") or []]
    if len(said) > 1:
        add("What has been said so far", "\n".join(said))

    stopped_in = ticket.get("prev_state") if ticket["state"] == "blocked" else ticket.get("stuck_from")
    reports = [r for r in state["reports"] if r["ticket"] == key]
    where = [f"It stopped while: {LABEL[stopped_in].lower()}."] if stopped_in in LABEL else []
    if reports:
        last = reports[-1]
        where.append(f"Last report: the {last['role']} said `{last['status']}`. {last.get('note', '')}".strip())
    add("Where the work stands", "\n".join(where))

    add("The developer's brief", read_note(folder, "brief.md"))

    if jira:
        try:
            found = plan.sections(jira.description_text(key))
            for name in ON_THE_TICKET:
                add(f"On the ticket: {name}", found.get(name.lower(), ""))
        except ApiError:
            add("On the ticket", "The ticket could not be read from Jira just now.")

    if ticket.get("mr_iid"):
        mr = ticket.get("mr") or {}
        lines = [f"!{ticket['mr_iid']} {ticket.get('mr_url', '')}".strip()]
        if mr.get("pipeline"):
            lines.append(f"Pipeline: {mr['pipeline']}")
        if mr.get("approved_by"):
            lines.append("Approved by: " + ", ".join(shown(cfg, user) for user in mr["approved_by"]))
        add("The merge request", "\n".join(lines))

    events = [event for event in state["events"] if event["ticket"] == key][-15:]
    log = [
        f"{event['at'][11:16]} {shown(cfg, event['from'])}{' to ' + shown(cfg, event['to']) if event['to'] else ''}: {event['text']}"
        for event in events
    ]
    add("Messages about this ticket", "\n".join(log))

    notes = sorted(path.name for path in folder.iterdir() if path.is_file())
    add("Files in the notes folder", "\n".join(notes))
    return parts
