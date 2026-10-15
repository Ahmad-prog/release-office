"""The shape of a ticket the office can run, and the check that says whether one is ready.

The planner writes a ticket in this shape. The developer builds from it, and the reviewer
judges a merge request against it: the requirements and decisions by their ids, and drift by
the two scope sections.

A ready ticket is verified, testable and settled: its facts carry their source and its paths
exist in the repo, every requirement says how to check it, and nothing in it is still open.
"""

import re

SECTIONS = (
    "Goal",
    "Problem",
    "Solution",
    "Requirements",
    "Decisions",
    "In scope",
    "Out of scope",
    "Change type",
    "How to verify",
    "Docs to update",
    "Risks",
    "Facts checked",
    "Open questions",
    "For the office",
)
MUST_SAY_SOMETHING = ("Goal", "Problem", "Solution", "In scope", "Out of scope", "How to verify")
CHANGE_TYPES = ("No output change", "Output change", "Tooling, CI or docs")
# What the developer and the reviewer act on. These state things, so none may read as a question.
BINDING = ("Requirements", "Decisions", "In scope", "Out of scope", "Change type", "How to verify", "Facts checked")
UNSETTLED = re.compile(
    r"\b(tbd|tbc|todo|to be (?:decided|confirmed|determined|defined|checked|verified)|not sure|unsure|unverified"
    r"|not (?:yet )?(?:verified|checked|confirmed|decided)|needs? (?:to be |to )?(?:check|confirm|verif)\w*"
    r"|assum\w+|presumably|probably|maybe|perhaps|i think|i believe|should be fine)\b|\?\?+",
    re.I,
)
NOT_A_SOURCE = re.compile(r"source:\s*(?:from\s+)?(?:my\s+)?(?:memory|recollection)\b", re.I)
CODE_FILES = (".py", ".yml", ".yaml", ".md", ".toml", ".sh", ".sql", ".json", ".cfg", ".lock")
CHECKED = "Paths checked:"
ORIGINAL = "Original description"

EXAMPLE = """## Goal
The weekly sales report shows the region and the currency of every order line, so a reader can tell a real change in revenue from a currency swing without opening the database.

## Problem
The report lists each order line and its amount, but not the region or the currency behind it. On PROJ-104 a reader had to query the database to find both.

## Solution
Carry the region and the currency from the order data the report already loads into each row, and show them as two columns.

## Requirements
- **R1.** Every row of the report in `report.html` has a Region column. Check: `tests/test_report.py` asserts the column and its value for a known order.
- **R2.** Every row has a Currency column holding the currency code read from the order. Check: the same test asserts it.
- **R3.** `report.json` carries both values on each row. Check: a test loads the JSON and compares.
- **R4.** A row with no region shows an empty cell and the report still renders. Check: an edge-case test.

## Decisions
- **D1.** Read the currency from the order data, not from the exchange-rate service, because the report has the order data in hand and a service call can fail.
- **D2.** Do not add the two columns to the summary tab in this ticket.

## In scope
- `src/reports/sales.py`
- `tests/test_report.py` (new)
- `docs/reports.md`

## Out of scope
- The pricing rules under `src/pricing/`.
- The summary tab.
- Anything outside `src/reports` and its test.

## Change type
Tooling, CI or docs

## How to verify
- Tests: `python -m pytest tests/test_report.py -q`
- By hand: open the `report.html` of one saved week and read the two columns.

## Docs to update
- `docs/reports.md`, the columns section.

## Risks
- None.

## Facts checked
- **F1.** The report rows are rendered by `src/reports/sales.py`. Source: read the file on the main branch.
- **F2.** No test covers the report today. Source: `git ls-tree --name-only origin/main:tests/` lists no `test_report.py`.
- **F3.** The tests run with `python -m pytest`. Source: the test job in the CI configuration.
- **F4.** The order data the report loads already holds the region and the currency. Source: read `src/reports/sales.py` on the main branch.

## Open questions
None.

## For the office
- Repo: your-repo
- Depends on: None
"""


KNOWN = {name.lower(): name for name in SECTIONS}
ORDER = ", ".join(SECTIONS)


def _title(line):
    heading = re.match(r"#{1,6}\s+(.*)", line)
    return heading.group(1).strip().strip("*:").strip().lower() if heading else None


def outline(text):
    """What comes before the first section, the sections in the order they appear, and their text.

    Only the office's own section titles count. Any other heading is part of the section it
    sits in. Reading stops at the original description.
    """
    before, order, found, current = [], [], {}, None
    for line in text.splitlines():
        title = _title(line)
        if line.startswith("--- ") or title == ORIGINAL.lower():
            break
        if title in KNOWN:
            current = title
            order.append(title)
            found.setdefault(title, "")
        elif current is None:
            before.append(line)
        else:
            found[current] += line + "\n"
    return "\n".join(before).strip(), order, found


def sections(text):
    """The text under each of the office's sections, by lower-cased title."""
    return outline(text)[2]


def items(body):
    """The list items of a section. An indented line continues the item above it."""
    out = []
    for line in body.splitlines():
        if re.match(r"([-*]|\d+\.)\s+", line):
            out.append(re.sub(r"^([-*]|\d+\.)\s+", "", line).strip())
        elif out and line.startswith(" ") and line.strip():
            out[-1] += " " + line.strip()
    return out


def is_none(body):
    plain = re.sub(r"^[-*]\s+", "", body.strip()).strip().rstrip(".").lower()
    return plain in ("", "none", "n/a", "nothing")


def _prose(body):
    """The text without what is quoted in backticks, so a quoted log line cannot trip a check."""
    return re.sub(r"`[^`\n]*`", " ", re.sub(r"```.*?```", " ", body, flags=re.S))


def _after(body, label):
    """What follows a label such as "Tests:" in a section, or None when the label is not there."""
    for entry in items(body) + [line.strip() for line in body.splitlines()]:
        found = re.match(rf"\**\s*{label}\**\s*(.*)", entry, re.I)
        if found:
            return found.group(1).strip()
    return None


def _ids(entries, letter):
    return [found.group(1) for entry in entries if (found := re.match(rf"\**\s*({letter}\d+)\.", entry))]


def check(text):
    """Everything that stops this ticket from being ready for the office. Empty means ready.

    The sections must all be there, once each, in the office's order, with nothing above the first.
    """
    before, order, found = outline(text)
    if not found:
        return [f"The ticket has none of the office's sections. It needs these, in this order: {ORDER}."]

    def body(name):
        return found.get(name.lower(), "").strip()

    problems = [f'The section "{name}" is missing.' for name in SECTIONS if name.lower() not in found]
    if before:
        problems.append('The ticket must start with the "Goal" section. Move the text above it into a section.')
    seen = []
    for title in order:
        if title in seen:
            problems.append(f'The section "{KNOWN[title]}" appears twice.')
        else:
            seen.append(title)
    expected = [name.lower() for name in SECTIONS if name.lower() in seen]
    if seen != expected:
        at = next(index for index, pair in enumerate(zip(seen, expected, strict=True)) if pair[0] != pair[1])
        problems.append(
            f'The sections are out of order: "{KNOWN[seen[at]]}" is where "{KNOWN[expected[at]]}" should be. '
            f"The order is: {ORDER}."
        )
    problems += [f'"{name}" is empty.' for name in MUST_SAY_SOMETHING if name.lower() in found and is_none(body(name))]

    if "requirements" in found:
        entries = items(body("Requirements"))
        if not entries:
            problems.append("There is no requirement. Write each as a list item: **R1.** what must be true. Check: how to tell.")
        elif _ids(entries, "R") != [f"R{n}" for n in range(1, len(entries) + 1)]:
            problems.append("Number the requirements R1, R2, R3 in order, one list item each.")
        else:
            for entry, name in zip(entries, _ids(entries, "R"), strict=True):
                if not re.search(r"check:\s*\S", entry, re.I):
                    problems.append(f'{name} does not say how to check it. End it with "Check: ...".')

    if "decisions" in found and not is_none(body("Decisions")):
        entries = items(body("Decisions"))
        if _ids(entries, "D") != [f"D{n}" for n in range(1, len(entries) + 1)] or not entries:
            problems.append('Number the decisions D1, D2, D3 in order, one list item each, or write "None."')

    if "change type" in found:
        kind = next((line.strip().strip("*-").strip() for line in body("Change type").splitlines() if line.strip()), "")
        if kind not in CHANGE_TYPES:
            problems.append('"Change type" must start with one of: ' + "; ".join(CHANGE_TYPES) + ".")
        elif kind == "Output change" and not re.search(r"expected difference:\s*\S", body("Change type"), re.I):
            problems.append('An output change must state it: add "Expected difference: ..." under "Change type".')

    if "how to verify" in found:
        for label in ("Tests:",):
            said = _after(body("How to verify"), label)
            skipped = re.match(r"(not applicable|n/a|none)\b[\s.:,;-]*(.*)", said or "", re.I)
            if said is None:
                problems.append(f'"How to verify" has no "{label}" line.')
            elif not said:
                problems.append(f'"{label}" in "How to verify" says nothing. Give the command, or "Not applicable" and why.')
            elif skipped and len(skipped.group(2)) < 4:
                problems.append(f'"{label}" says not applicable without saying why.')
            elif label == "Tests:" and not skipped and "`" not in said:
                problems.append('"Tests:" must give the exact command, in backticks.')

    if "facts checked" in found:
        entries = items(body("Facts checked"))
        if not entries:
            problems.append(
                '"Facts checked" is empty. List each fact the plan rests on: **F1.** the fact. Source: the file, command or query that showed it.'
            )
        elif _ids(entries, "F") != [f"F{n}" for n in range(1, len(entries) + 1)]:
            problems.append("Number the facts F1, F2, F3 in order, one list item each.")
        else:
            for entry, name in zip(entries, _ids(entries, "F"), strict=True):
                if not re.search(r"source:\s*\S", entry, re.I):
                    problems.append(f'{name} does not say where it was checked. End it with "Source: ...".')
                elif NOT_A_SOURCE.search(entry):
                    problems.append(f"{name} was not checked: memory is not a source. Look it up and name the file, command or query.")

    if "open questions" in found and not is_none(body("Open questions")):
        problems.append("Open questions remain. Settle each one with the owner, then write \"None.\"")

    for name in SECTIONS:
        if name == "Open questions" or name.lower() not in found:
            continue
        prose = _prose(body(name))
        loose = UNSETTLED.search(prose)
        if loose:
            problems.append(
                f'"{name}" is not settled: it says "{loose.group(0)}". Check it and state it as a fact, '
                "or ask the owner and write the answer down as a decision."
            )
        elif name in BINDING and re.search(r"\?(?=\s|$)", prose):
            problems.append(f'"{name}" asks a question. Settle it with the owner and write the answer as a statement.')

    if "for the office" in found:
        office = body("For the office").lower()
        for label in ("Repo:",):
            if label.lower() not in office:
                problems.append(f'"For the office" has no "{label}" line.')
    return problems


def has_plan(text):
    found = sections(text)
    return "requirements" in found and "for the office" in found


def with_original(new, old):
    """The new description, with whatever the ticket said before kept underneath it."""
    old = (old or "").strip()
    if not old or re.search(rf"^#{{1,6}}\s+{ORIGINAL}\s*$", new, re.M):
        return new
    kept = re.split(rf"^#{{1,6}}\s+{ORIGINAL}\s*$", old, maxsplit=1, flags=re.M)
    if len(kept) == 2:
        earlier = kept[1].strip()
    elif has_plan(old):
        return new
    else:
        # Headings in the old text become bold lines, so they cannot be mistaken for plan sections.
        earlier = re.sub(r"^#{1,6}\s+(.*)$", r"**\1**", old, flags=re.M)
    return f"{new.rstrip()}\n\n## {ORIGINAL}\n\n{earlier}\n" if earlier else new


SPAN = re.compile(r"`([^`\n]+)`(\s*\(new\b)?", re.I)


def _path(token, tops):
    """A repo path named by this word, and whether it starts at the top of the repo."""
    token = re.sub(r"^(?:\.{1,2}/)+", "", token.strip("\"'()[]{},;"))
    token = re.sub(r":\d+(-\d+)?$", "", token.split("::")[0]).rstrip(".,:;")
    wild = re.search(r"[*<{$?]", token)
    if wild:
        token = token[: wild.start()].rpartition("/")[0]
    token = token.rstrip("/")
    if not token or "://" in token or token.startswith(("/", "~", "-")):
        return "", False
    if token.split("/")[0] in tops:
        return token, True
    return (token, False) if "/" in token and token.endswith(CODE_FILES) else ("", False)


def check_repo(text, tops, exists, where="staging", protected=()):
    """What the ticket says about the repo that is not so, and how many paths were looked up.

    ``tops`` holds the names at the top of the repo and ``exists`` says whether a path is there.
    A path this ticket will create is written with (new) after it.
    """
    found = outline(text)[2]
    named = []
    for name in SECTIONS:
        for span in SPAN.finditer(found.get(name.lower(), "")):
            for token in span.group(1).split():
                path, rooted = _path(token, tops)
                if path:
                    named.append((name, path, rooted, bool(span.group(2))))
    fresh = {path for _, path, _, new in named if new}
    problems, seen = [], set()
    for name, path, rooted, _ in named:
        if (path, name == "In scope") in seen:
            continue
        first = path not in {p for p, _ in seen}
        seen.add((path, name == "In scope"))
        if name == "In scope" and any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in protected):
            problems.append(f"`{path}` is a protected path and cannot be in scope. That change is the owner's own work.")
        if not first:
            continue
        if not rooted:
            problems.append(f'`{path}`, under "{name}", does not start at the top of the repo. Write the whole path.')
        elif path in fresh and exists(path):
            problems.append(f'`{path}` is marked (new) under "{name}", but it already exists on {where}.')
        elif path not in fresh and not exists(path):
            problems.append(f'`{path}`, under "{name}", does not exist on {where}. Correct it, or write (new) after it if this ticket creates it.')
    if "in scope" in found and not any(name == "In scope" and rooted for name, _, rooted, _ in named):
        problems.append('"In scope" names no path in the repo. Name the folders and files, each in backticks, from the top of the repo.')
    return problems, len({path for path, _ in seen})


def stamped(text, line):
    """The plan with one closing line that says what its paths were checked against."""
    if re.search(rf"^#{{1,6}}\s+{ORIGINAL}\s*$", text, re.M):
        return text
    kept = [row for row in text.rstrip().splitlines() if not re.match(rf"[-*]\s+{CHECKED}", row)]
    return "\n".join(kept) + f"\n- {CHECKED} {line}\n"
