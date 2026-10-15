"""The ticket plan: what counts as ready, and that it survives a trip through Jira's format."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from office import config, plan
from office.jira import flatten, to_adf
from tests.test_office import KEY, Case

READY = plan.EXAMPLE


def without(title):
    """The example with one section cut out."""
    kept, skipping = [], False
    for line in READY.splitlines():
        if line.startswith("## "):
            skipping = line == f"## {title}"
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def swapped(first, second):
    """The example with two neighbouring sections in the wrong order."""
    a, b = READY.index(f"## {first}"), READY.index(f"## {second}")
    end = READY.index("## ", b + 3)
    return READY[:a] + READY[b:end] + READY[a:b] + READY[end:]


class Check(unittest.TestCase):
    def test_the_example_is_ready(self):
        self.assertEqual(plan.check(READY), [])

    def test_each_gap_is_named(self):
        gaps = [
            (without("Out of scope"), 'The section "Out of scope" is missing.'),
            (READY.replace("- **R1.** ", "- "), "Number the requirements R1, R2, R3 in order"),
            (READY.replace("- **R2.** ", "- **R7.** "), "Number the requirements R1, R2, R3 in order"),
            (READY.replace(" Check: the same test asserts it.", ""), "R2 does not say how to check it"),
            (READY.replace("## Open questions\nNone.", "## Open questions\n- Should the summary tab get them too?"), "Open questions remain"),
            (READY.replace("Tooling, CI or docs", "Refactor"), '"Change type" must start with one of'),
            (READY.replace("Tooling, CI or docs", "Output change"), "Expected difference"),
            (READY.replace("- Repo: your-repo\n", ""), 'no "Repo:" line'),
            (READY.replace("- **D2.** ", "- "), "Number the decisions"),
            ("Just add two columns to the report.", "none of the office's sections"),
            ("Some notes first.\n\n" + READY, 'must start with the "Goal" section'),
            (READY + "\n## Risks\n- None.\n", 'The section "Risks" appears twice.'),
            (swapped("Requirements", "Decisions"), 'out of order: "Decisions" is where "Requirements" should be'),
            (swapped("Goal", "Problem"), 'out of order: "Problem" is where "Goal" should be'),
            # verified: every fact the plan rests on carries its source
            (without("Facts checked"), 'The section "Facts checked" is missing.'),
            (READY.replace(" Source: read the file on the main branch.", ""), "F1 does not say where it was checked"),
            (READY.replace("Source: read the file on the main branch.", "Source: from memory."), "memory is not a source"),
            (READY.replace("- **F3.** ", "- "), "Number the facts F1, F2, F3 in order"),
            # testable: the way to test is written down
            (READY.replace(" Check: the same test asserts it.", " Check:"), "R2 does not say how to check it"),
            (READY.replace("- Tests: `python -m pytest tests/test_report.py -q`", "- Tests: run the report tests"), "exact command, in backticks"),
            (READY.replace("- Tests: `python -m pytest tests/test_report.py -q`", "- Tests:"), '"Tests:" in "How to verify" says nothing'),
            (READY.replace("- Tests: `python -m pytest tests/test_report.py -q`", "- Tests: Not applicable."), "not applicable without saying why"),
            # settled: nothing in it is still open
            (READY.replace("from the order data, not from the exchange-rate service,", "from the order data, TBD whether the service too,"), '"Decisions" is not settled: it says "TBD"'),
            (READY.replace("The summary tab.", "Probably the summary tab."), '"Out of scope" is not settled'),
            (READY.replace("and the report still renders.", "or should it say n/a?"), '"Requirements" asks a question'),
            (READY.replace("Carry the region", "I assume we carry the region"), '"Solution" is not settled: it says "assume"'),
        ]
        for text, expected in gaps:
            problems = plan.check(text)
            self.assertTrue(any(expected in problem for problem in problems), f"expected {expected!r} in {problems}")

    def test_an_output_change_is_ready_once_it_states_the_difference(self):
        text = READY.replace("Tooling, CI or docs", "Output change\nExpected difference: `Region` is filled on every row; no other column changes.")
        self.assertEqual(plan.check(text), [])

    def test_a_heading_inside_a_section_is_just_part_of_it(self):
        self.assertEqual(plan.check(READY.replace("## Requirements\n", "## Requirements\n### The report\n")), [])

    def test_no_decisions_is_allowed_when_it_says_so(self):
        start, end = READY.index("## Decisions"), READY.index("## In scope")
        self.assertEqual(plan.check(READY[:start] + "## Decisions\nNone.\n\n" + READY[end:]), [])


TOPS = {"src", "tests", "docs", "restricted", ".gitlab-ci.yml"}
THERE = {"src/reports/sales.py", "docs/reports.md", "src/pricing", "src/reports", "tests", ".gitlab-ci.yml"}


def repo_faults(text, there=frozenset(THERE)):
    return plan.check_repo(text, TOPS, lambda path: path in there, "staging", ("restricted",))


class Repo(unittest.TestCase):
    def test_every_path_the_example_names_is_looked_up(self):
        self.assertEqual(repo_faults(READY), ([], 5))

    def test_a_question_mark_in_a_quoted_log_line_is_not_a_question(self):
        self.assertEqual(plan.check(READY.replace("has a Region column.", "has a Region column, where the log said `region?`.")), [])

    def test_what_the_ticket_says_about_the_repo_must_be_so(self):
        gaps = [
            (READY.replace("`src/reports/sales.py`\n", "`src/reports/sales/`\n"), "`src/reports/sales`, under \"In scope\", does not exist on staging"),
            (READY.replace("`docs/reports.md`\n", "`docs/reports.md` (new)\n"), "is marked (new) under \"In scope\", but it already exists"),
            (READY.replace("Check: `tests/test_report.py` asserts", "Check: `unit/test_other.py` asserts"), "does not start at the top of the repo"),
            (READY.replace("- `docs/reports.md`\n\n## Out of scope", "- `restricted/data/README.md`\n\n## Out of scope"), "is a protected path and cannot be in scope"),
            (READY.replace("tests/test_report.py -q`", "tests/test_report.py -q tests/test_gone.py::test_it`"), "`tests/test_gone.py`, under \"How to verify\", does not exist"),
        ]
        for text, expected in gaps:
            problems = repo_faults(text)[0]
            self.assertTrue(any(expected in problem for problem in problems), f"expected {expected!r} in {problems}")

    def test_scope_must_name_a_place_in_the_repo(self):
        start, end = READY.index("## In scope"), READY.index("## Out of scope")
        vague = READY[:start] + "## In scope\n- The sales report.\n\n" + READY[end:]
        self.assertIn('"In scope" names no path in the repo', " ".join(repo_faults(vague)[0]))

    def test_a_new_file_is_new_wherever_it_is_named_and_a_pattern_checks_its_folder(self):
        text = READY.replace("- `docs/reports.md`\n\n## Out of scope", "- `src/reports/*.py`\n\n## Out of scope")
        self.assertEqual(repo_faults(text)[0], [])

    def test_the_ticket_says_what_its_paths_were_checked_against(self):
        once = plan.stamped(READY, "7 path(s) looked up on staging at abc1234")
        twice = plan.stamped(once, "7 path(s) looked up on staging at def5678")
        self.assertTrue(twice.rstrip().endswith("- Paths checked: 7 path(s) looked up on staging at def5678"))
        self.assertEqual(twice.count("Paths checked:"), 1)
        self.assertEqual(plan.check(twice), [])


class ThroughJira(unittest.TestCase):
    def test_the_plan_reads_back_the_same_from_jiras_format(self):
        back = flatten(to_adf(READY))
        self.assertEqual(plan.check(back), [])
        for piece in ("## Requirements", "- **R1.** Every row", "`report.html`", "- Repo: your-repo"):
            self.assertIn(piece, back)

    def test_links_and_code_blocks_survive(self):
        text = "See [the runbook](https://example.com/run).\n\n```\nuv run pytest\n```\n\n1. first\n2. second"
        back = flatten(to_adf(text))
        self.assertIn("[the runbook](https://example.com/run)", back)
        self.assertIn("```\nuv run pytest\n```", back)
        self.assertIn("1. first\n2. second", back)


class Original(unittest.TestCase):
    def test_what_the_ticket_said_before_is_kept_and_does_not_confuse_the_check(self):
        old = "## Problem\nThe old words from whoever raised it.\n\n## Requirements\n- make it work"
        merged = plan.with_original(READY, old)
        self.assertIn("## Original description", merged)
        self.assertIn("**Problem**", merged)
        self.assertIn("The old words from whoever raised it.", merged)
        self.assertEqual(plan.check(merged), [])

    def test_a_second_update_keeps_the_same_original_once(self):
        first = plan.with_original(READY, "The old words.")
        second = plan.with_original(READY.replace("two columns", "three columns"), first)
        self.assertEqual(second.count("## Original description"), 1)
        self.assertIn("The old words.", second)
        self.assertIn("three columns", second)

    def test_an_empty_ticket_gains_nothing(self):
        self.assertEqual(plan.with_original(READY, ""), READY)


class Gate(Case):
    def test_a_ticket_out_of_shape_is_rejected_at_once_and_starts_by_itself_when_fixed(self):
        office = self.office()
        office.jira.tickets[KEY]["text"] = swapped("Requirements", "Decisions")
        office.open_office()
        self.assertEqual(office.ticket()["state"], "needs_plan")
        self.assertIsNone(office.ticket()["todo"])
        self.assertIn("out of order", office.ticket()["plan_problems"][0])
        # The owner is told on the page, once, however many times the manager looks. Nothing goes on Jira.
        office.manager.tick()
        office.manager.tick()
        told = [event for event in office.store.read()["events"] if event["text"].startswith("Not started.")]
        self.assertEqual(len(told), 1)
        self.assertIn("out of order", told[0]["text"])
        self.assertEqual(office.jira.comments, [])

        office.jira.tickets[KEY]["text"] = READY
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "working")
        self.assertEqual(office.ticket()["todo"]["role"], "developer")

    def test_a_different_fault_is_told_to_the_owner_again(self):
        office = self.office()
        office.jira.tickets[KEY]["text"] = "Add two columns to the report."
        office.open_office()
        office.jira.tickets[KEY]["text"] = swapped("Goal", "Problem")
        office.manager.tick()
        told = [event for event in office.store.read()["events"] if event["text"].startswith("Not started.")]
        self.assertEqual(len(told), 2)
        self.assertEqual(office.jira.comments, [])
        self.assertEqual(office.ticket()["state"], "needs_plan")

    def test_a_fixed_ticket_waits_its_turn_and_goes_ahead_of_new_ones(self):
        office = self.office()
        office.jira.tickets[KEY]["text"] = "Add two columns to the report."
        office.open_office()
        office.jira.move("DEMO-102", "In Progress")
        office.manager.tick()
        office.jira.tickets[KEY]["text"] = READY
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "queued")
        office.jira.move("DEMO-103", "In Progress")
        office.jira.fix("DEMO-103")
        office.manager.tick()
        self.assertNotIn("DEMO-103", office.store.read()["tickets"])
        # The ticket in hand reaches a person, and the fixed one starts next.
        office.say("developer", "built", "DEMO-102")
        office.say("coauthor", "clean", "DEMO-102")
        iid = office.gitlab.open("DEMO-102", "staging")
        office.say("developer", "mr_open", "DEMO-102", mr=iid)
        office.say("reviewer", "passed", "DEMO-102")
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "working")

    def test_another_ticket_can_start_while_one_waits_to_be_fixed(self):
        office = self.office()
        office.jira.tickets[KEY]["text"] = "Add two columns to the report."
        office.open_office()
        office.jira.move("DEMO-102", "In Progress")
        office.manager.tick()
        self.assertEqual(office.ticket("DEMO-102")["state"], "working")

    def test_the_owner_can_start_one_without_a_plan(self):
        office = self.office()
        office.jira.tickets[KEY]["text"] = "Add two columns to the report."
        office.open_office()
        self.assertTrue(office.manager.resume(KEY, unplanned=True))
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "working")

    def test_the_gate_can_be_switched_off(self):
        office = self.office(jira__require_plan=False)
        office.jira.tickets[KEY]["text"] = "Add two columns to the report."
        office.open_office()
        self.assertEqual(office.ticket()["state"], "working")


class Tool(unittest.TestCase):
    def pretend_repo(self, folder):
        """A small repo holding what the example names, with a staging branch as origin has it."""
        repo = folder / "repo"
        for name in ("src/reports/sales.py", "docs/reports.md", "src/pricing/rules.py", "tests/test_other.py", ".gitlab-ci.yml"):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text("x\n", encoding="utf-8")
        who = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        for step in (["init", "-q"], ["add", "-A"], [*who, "commit", "-q", "-m", "x"], ["update-ref", "refs/remotes/origin/staging", "HEAD"]):
            subprocess.run(["git", *step], cwd=repo, check=True, capture_output=True)
        settings = folder / "office.config.json"
        settings.write_text(json.dumps({"repo": {"path": str(repo)}}), encoding="utf-8")
        return str(settings)

    def run_tool(self, *args, dry=True, role="planner"):
        with tempfile.TemporaryDirectory() as folder:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("OFFICE_", "JIRA_", "GITLAB_"))}
            env.update(OFFICE_STATE_DIR=folder, OFFICE_ENV_FILE=str(Path(folder) / "absent"), OFFICE_ROLE=role)
            env["OFFICE_CONFIG"] = self.pretend_repo(Path(folder))
            if dry:
                env["OFFICE_DRY"] = "1"
            ready, thin, wrong = Path(folder) / "ready.md", Path(folder) / "thin.md", Path(folder) / "wrong.md"
            ready.write_text(READY, encoding="utf-8")
            wrong.write_text(READY.replace("`src/reports/sales.py`\n", "`src/reports/sales/`\n"), encoding="utf-8")
            thin.write_text("Add two columns.", encoding="utf-8")
            cmd = [sys.executable, str(config.HOME / "officectl.py"), *[str(a).replace("READY", str(ready)).replace("THIN", str(thin)).replace("WRONG", str(wrong)) for a in args]]
            return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)

    def test_a_draft_can_be_checked_before_it_goes_anywhere(self):
        ready = self.run_tool("ticket-check", "--file", "READY")
        self.assertIn("Ready for the office. 5 path(s) looked up on staging at", ready.stdout)
        thin = self.run_tool("ticket-check", "--file", "THIN")
        self.assertEqual(thin.returncode, 1)
        self.assertIn("none of the office's sections", thin.stdout)

    def test_a_ticket_that_is_not_ready_is_not_created(self):
        refused = self.run_tool("ticket-create", "--summary", "Report: add two columns", "--description-file", "THIN")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("not ready for the office", refused.stderr)

    def test_a_ticket_that_names_a_path_the_repo_does_not_have_is_not_ready(self):
        checked = self.run_tool("ticket-check", "--file", "WRONG")
        self.assertEqual(checked.returncode, 1)
        self.assertIn("`src/reports/sales`, under \"In scope\", does not exist on staging", checked.stdout)
        refused = self.run_tool("ticket-create", "--summary", "Report: add two columns", "--description-file", "WRONG")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("does not exist on staging", refused.stderr)

    def test_only_the_owner_can_write_a_ticket_that_is_not_ready(self):
        planner = self.run_tool("ticket-create", "--summary", "Report: add two columns", "--description-file", "WRONG", "--draft")
        self.assertNotEqual(planner.returncode, 0)
        self.assertIn("Only the owner may do that", planner.stderr)
        owner = self.run_tool("ticket-create", "--summary", "Report: add two columns", "--description-file", "WRONG", "--draft", role="")
        self.assertIn("[dry run] would create a Story", owner.stdout)

    def test_the_planner_only_plans(self):
        refused = self.run_tool("tick", "--dry")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("the planner does not run `tick`", refused.stderr)

    def test_a_ready_ticket_would_be_created_in_the_sprint(self):
        done = self.run_tool("ticket-create", "--summary", "Sales report: show the region", "--description-file", "READY")
        self.assertIn("[dry run] would create a Story in PROJ, sprint Sprint 1", done.stdout)


if __name__ == "__main__":
    unittest.main()
