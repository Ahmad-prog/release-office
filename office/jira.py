"""Jira client: the few reads and writes the office needs, and text to and from Jira's document format."""

import base64
import re
from datetime import datetime

from office.api import ApiError, call

INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\))")
BLOCK_START = re.compile(r"(#{1,6}\s|[-*]\s|\d+\.\s|```)")


def _inline(node):
    kind, attrs = node.get("type"), node.get("attrs") or {}
    if kind == "text":
        text = node.get("text", "")
        for mark in node.get("marks") or []:
            if mark["type"] == "strong":
                text = f"**{text}**"
            elif mark["type"] == "code":
                text = f"`{text}`"
            elif mark["type"] == "link":
                text = f"[{text}]({(mark.get('attrs') or {}).get('href', '')})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return attrs.get("text", "")
    if kind == "inlineCard":
        return attrs.get("url", "")
    return "".join(_inline(child) for child in node.get("content") or [])


def flatten(node):
    """Atlassian Document Format to plain text, keeping headings and lists as markdown."""
    if isinstance(node, list):
        return "".join(flatten(item) for item in node)
    if not isinstance(node, dict):
        return ""
    kind, content = node.get("type"), node.get("content") or []
    if kind == "heading":
        level = int((node.get("attrs") or {}).get("level", 2))
        return "#" * level + " " + "".join(_inline(child) for child in content).strip() + "\n"
    if kind == "paragraph":
        return "".join(_inline(child) for child in content) + "\n"
    if kind in ("bulletList", "orderedList"):
        lines = []
        for index, item in enumerate(content, 1):
            first, *more = flatten(item.get("content") or []).rstrip("\n").split("\n")
            lines.append(f"{'-' if kind == 'bulletList' else f'{index}.'} {first}")
            lines += [f"  {line}" for line in more]
        return "\n".join(lines) + "\n"
    if kind == "codeBlock":
        return "```\n" + "".join(_inline(child) for child in content) + "\n```\n"
    if kind in ("tableCell", "tableHeader"):
        return flatten(content).strip().replace("\n", " ") + " | "
    if kind == "tableRow":
        return "| " + flatten(content).rstrip() + "\n"
    if kind == "rule":
        return "---\n"
    return flatten(content) if content else _inline(node)


def _marks(text):
    """Bold, inline code and links inside one line."""
    nodes = []
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        elif part.startswith("[") and part.endswith(")") and "](" in part:
            label, href = part[1:-1].split("](", 1)
            nodes.append({"type": "text", "text": label, "marks": [{"type": "link", "attrs": {"href": href}}]})
        else:
            nodes.append({"type": "text", "text": part})
    return nodes


def _paragraph(lines):
    content = []
    for index, line in enumerate(lines):
        if index:
            content.append({"type": "hardBreak"})
        content += _marks(line)
    return {"type": "paragraph", "content": content}


def to_adf(text):
    """Markdown-like text to the document format Jira's v3 API wants.

    Understands headings, bullet and numbered lists, fenced code, bold, inline code and links.
    """
    blocks, lines, index = [], text.replace("\r\n", "\n").split("\n"), 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("```"):
            code, index = [], index + 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            block = {"type": "codeBlock"}
            if code:
                block["content"] = [{"type": "text", "text": "\n".join(code)}]
            blocks.append(block)
            continue
        heading = re.match(r"(#{1,6})\s+(.*)", line)
        if heading:
            level = {"level": len(heading.group(1))}
            blocks.append({"type": "heading", "attrs": level, "content": _marks(heading.group(2).strip())})
            index += 1
            continue
        bullet = re.match(r"[-*]\s+", line)
        if bullet or re.match(r"\d+\.\s+", line):
            pattern = re.compile(r"[-*]\s+(.*)" if bullet else r"\d+\.\s+(.*)")
            items = []
            while index < len(lines):
                found = pattern.match(lines[index])
                if found:
                    items.append([found.group(1)])
                elif items and lines[index].startswith("  ") and lines[index].strip():
                    items[-1].append(lines[index].strip())
                else:
                    break
                index += 1
            kind = "bulletList" if bullet else "orderedList"
            rows = [{"type": "listItem", "content": [_paragraph(item)]} for item in items]
            blocks.append({"type": kind, "content": rows})
            continue
        paragraph = []
        while index < len(lines) and lines[index].strip() and not BLOCK_START.match(lines[index]):
            paragraph.append(lines[index])
            index += 1
        blocks.append(_paragraph(paragraph))
    return {"type": "doc", "version": 1, "content": blocks}


def parse_time(stamp):
    """Jira writes 2026-10-08T01:13:45.123-0700."""
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f%z")


class Jira:
    def __init__(self, base_url, email, token):
        self.base = base_url.rstrip("/")
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {"Authorization": "Basic " + auth, "Accept": "application/json"}

    def _call(self, method, path, params=None, body=None):
        return call(method, self.base + path, self.headers, params, body)[0]

    def url(self, key):
        return f"{self.base}/browse/{key}"

    # ---- reading

    def in_progress(self, sprint, status):
        """The owner's stories and tasks in the sprint that sit in ``status``. Epics are left out."""
        jql = (
            f'assignee = currentUser() AND sprint = "{sprint}" AND status = "{status}" '
            "AND issuetype != Epic ORDER BY updated ASC"
        )
        data = self._call(
            "GET", "/rest/api/3/search/jql", {"jql": jql, "fields": "summary,issuetype,updated", "maxResults": 50}
        )
        return [
            {
                "key": issue["key"],
                "summary": issue["fields"]["summary"],
                "type": issue["fields"]["issuetype"]["name"],
                "updated": issue["fields"].get("updated", ""),
            }
            for issue in (data or {}).get("issues", [])
        ]

    def issue(self, key, changelog=False):
        params = {"expand": "changelog"} if changelog else None
        return self._call("GET", f"/rest/api/3/issue/{key}", params)

    def summary(self, key):
        return self._call("GET", f"/rest/api/3/issue/{key}", {"fields": "summary"})["fields"]["summary"]

    def status(self, key):
        return self._call("GET", f"/rest/api/3/issue/{key}", {"fields": "status"})["fields"]["status"]["name"]

    def description_text(self, key):
        """The description alone, as text. This is what the office's plan check reads."""
        fields = self._call("GET", f"/rest/api/3/issue/{key}", {"fields": "description"})["fields"]
        return flatten(fields.get("description")).strip()

    def entered_status_at(self, key, status):
        """When the ticket last moved into ``status``, or None."""
        latest = None
        for history in (self.issue(key, changelog=True).get("changelog") or {}).get("histories", []):
            for item in history["items"]:
                if item["field"] == "status" and (item.get("toString") or "").lower() == status.lower():
                    moved = parse_time(history["created"])
                    latest = moved if latest is None or moved > latest else latest
        return latest

    def sprint_id(self, board_id, name):
        """The id of the active sprint with this name, which is what the sprint field takes."""
        found = self._call("GET", f"/rest/agile/1.0/board/{board_id}/sprint", {"state": "active"}) or {}
        for sprint in found.get("values", []):
            if sprint["name"] == name:
                return sprint["id"]
        raise ApiError(404, f"no active sprint named {name} on board {board_id}")

    def ticket_text(self, key):
        """The whole ticket as plain text, for an agent to read."""
        issue = self._call(
            "GET",
            f"/rest/api/3/issue/{key}",
            {"fields": "summary,description,issuetype,status,comment,issuelinks,parent,assignee,reporter"},
        )
        fields = issue["fields"]
        name = lambda person: (person or {}).get("displayName", "nobody")  # noqa: E731
        lines = [
            f"{key} · {fields['issuetype']['name']} · {fields['status']['name']}",
            f"Summary: {fields['summary']}",
            f"Assignee: {name(fields.get('assignee'))} · Reporter: {name(fields.get('reporter'))}",
            f"Link: {self.url(key)}",
        ]
        if fields.get("parent"):
            lines.append(f"Parent: {fields['parent']['key']} {fields['parent']['fields'].get('summary', '')}")
        for link in fields.get("issuelinks") or []:
            other = link.get("outwardIssue") or link.get("inwardIssue") or {}
            kind = link["type"]["outward"] if link.get("outwardIssue") else link["type"]["inward"]
            lines.append(f"Linked: {kind} {other.get('key', '')} {(other.get('fields') or {}).get('summary', '')}")
        description = flatten(fields.get("description")).strip()
        lines += ["", "--- Description ---", description or "(empty)"]
        comments = (fields.get("comment") or {}).get("comments", [])
        lines += ["", f"--- Comments ({len(comments)}) ---"]
        for item in comments:
            lines.append(f"[{item['created'][:10]} {name(item.get('author'))}] {flatten(item.get('body')).strip()}")
        return "\n".join(lines)

    # ---- writing

    def transition(self, key, to_status):
        """Move the ticket to ``to_status``. True when it is there afterwards."""
        if self.status(key).lower() == to_status.lower():
            return True
        choices = self._call("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
        match = next((c for c in choices if c["to"]["name"].lower() == to_status.lower()), None)
        if not match:
            return False
        self._call("POST", f"/rest/api/3/issue/{key}/transitions", body={"transition": {"id": match["id"]}})
        return True

    def comment(self, key, text):
        self._call("POST", f"/rest/api/3/issue/{key}/comment", body={"body": to_adf(text)})

    def create(self, project, issue_type, summary, description, sprint_field="", sprint_id=None, parent=""):
        """A new ticket, assigned to the token's owner. It starts in the project's first status."""
        fields = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": to_adf(description),
            "assignee": {"accountId": self._call("GET", "/rest/api/3/myself")["accountId"]},
        }
        if sprint_field and sprint_id:
            fields[sprint_field] = sprint_id
        if parent:
            fields["parent"] = {"key": parent}
        return self._call("POST", "/rest/api/3/issue", body={"fields": fields})["key"]

    def update(self, key, summary="", description=""):
        fields = {}
        if summary:
            fields["summary"] = summary
        if description:
            fields["description"] = to_adf(description)
        if fields:
            self._call("PUT", f"/rest/api/3/issue/{key}", body={"fields": fields})
