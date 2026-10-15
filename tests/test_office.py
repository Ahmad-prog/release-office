"""The manager's flow, walked end to end against a pretend Jira and GitLab."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import server
from office import config, context
from office.api import ApiError
from office.demo import FakeGitLab, FakeJira
from office.facts import classify, stamp
from office.jira import flatten, to_adf
from office.manager import Manager, answer_question
from office.sessions import ClaudeLauncher, ManualLauncher, _finish, describe
from office.state import Store, add_report, new_ticket
from officectl import TICKET_ID

OWNER, REVIEWER, MERGER = "owner", "second.reviewer", "maintainer"
KEY = "DEMO-101"


class Office:
    """A manager wired to pretend systems, with helpers to play each role's report."""

    def __init__(self, **overrides):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["state_dir"] = self.tmp.name
        self.cfg["people"]["human_reviewer"] = REVIEWER
        # Most tests walk the flow with a human review and no reviewer agent; ReviewerAgentOnly walks the default.
        self.cfg["human_review"]["enabled"], self.cfg["agent_review"]["enabled"] = True, False
        for path, value in overrides.items():
            section, name = path.split("__")
            self.cfg[section][name] = value
        self.store = Store(self.tmp.name)
        self.jira = FakeJira()
        self.gitlab = FakeGitLab(OWNER)
        self.manager = Manager(self.cfg, self.store, self.jira, self.gitlab, ManualLauncher())

    def ticket(self, key=KEY):
        return self.store.read()["tickets"][key]

    def say(self, role, status, key=KEY, **extra):
        self.store.update(lambda state: add_report(state, key, role, status, **extra))
        return self.manager.tick()

    def open_office(self, key=KEY):
        self.manager.start()
        self.jira.move(key, "In Progress")
        self.manager.tick()

    def to_merge_request(self, key=KEY):
        """Play the developer and the co-author up to an open merge request."""
        self.open_office(key)
        self.say("developer", "built", key)
        self.say("coauthor", "clean", key)
        iid = self.gitlab.open(key, "staging")
        self.say("developer", "mr_open", key, mr=iid)
        return iid

    def to_human_review(self, key=KEY):
        iid = self.to_merge_request(key)
        if self.cfg["agent_review"]["enabled"]:
            self.say("reviewer", "passed", key)
        return iid


class Case(unittest.TestCase):
    def office(self, **overrides):
        made = Office(**overrides)
        self.addCleanup(made.tmp.cleanup)
        return made


class HappyPath(Case):
    def test_a_ticket_travels_from_in_progress_to_staging(self):
        office = self.office(agent_review__enabled=True)
        office.open_office()
        self.assertEqual(office.ticket()["state"], "working")
        self.assertEqual(office.ticket()["todo"]["role"], "developer")

        office.say("developer", "built")
        self.assertEqual(office.ticket()["state"], "co_review")
        office.say("coauthor", "findings", count=2, file="coauthor-1.md")
        self.assertEqual(office.ticket()["state"], "working")
        self.assertIn("coauthor-1.md", office.ticket()["todo"]["message"])
        office.say("developer", "built")
        office.say("coauthor", "clean")
        self.assertEqual(office.ticket()["state"], "opening_mr")

        iid = office.gitlab.open(KEY, "staging")
        office.say("developer", "mr_open", mr=iid)
        self.assertEqual(office.ticket()["state"], "in_review")
        self.assertEqual(office.jira.status(KEY), "Completed")

        office.gitlab.reviewer_posts(iid, 2)
        office.say("reviewer", "threads_posted", count=2)
        self.assertEqual(office.ticket()["state"], "fixing")
        office.say("developer", "fixed")
        self.assertEqual(office.ticket()["rounds"]["review"], 2)
        office.gitlab.reviewer_resolves(iid)
        office.say("reviewer", "passed")

        # The temporary step: a person reviews after the reviewer agent.
        self.assertEqual(office.ticket()["state"], "human_review")
        self.assertEqual(office.gitlab.mrs[iid]["reviewers"], [REVIEWER])
        self.assertEqual(office.gitlab.mrs[iid]["assignees"], [])
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "human_review")

        office.gitlab.approve(iid, REVIEWER)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "waiting_merge")
        self.assertEqual(office.gitlab.mrs[iid]["assignees"], [MERGER])
        self.assertEqual(office.jira.status(KEY), "Reviewed")

        office.gitlab.merge(iid)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "on_staging")
        self.assertEqual(office.jira.status(KEY), "Deployed to Staging")

    def test_the_last_coauthor_round_goes_straight_to_the_merge_request(self):
        office = self.office()
        office.open_office()
        for _ in range(2):
            office.say("developer", "built")
            office.say("coauthor", "findings", count=1, file="f.md")
        self.assertEqual(office.ticket()["state"], "opening_mr")


class People(Case):
    def test_a_persons_comment_goes_to_the_developer_and_back(self):
        office = self.office()
        iid = office.to_human_review()
        office.gitlab.person_comments(iid, REVIEWER)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "fixing_person")
        self.assertIn("from the human reviewer", office.ticket()["todo"]["message"])
        self.assertNotIn(REVIEWER, office.ticket()["todo"]["message"])

        office.gitlab.developer_replies(iid)
        office.say("developer", "fixed")
        self.assertEqual(office.ticket()["state"], "human_review")
        # The developer answered, so the thread now waits on the person, not on the developer.
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "human_review")

    def test_only_the_named_reviewers_approval_counts(self):
        office = self.office()
        iid = office.to_human_review()
        office.gitlab.approve(iid, OWNER)
        office.gitlab.approve(iid, "someone_else")
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "human_review")

    def test_an_open_thread_holds_the_handover_even_after_approval(self):
        office = self.office()
        iid = office.to_human_review()
        office.gitlab.add_thread(iid, MERGER, "One more thing.")
        office.manager.tick()
        office.gitlab.developer_replies(iid)
        office.say("developer", "fixed")
        office.gitlab.approve(iid, REVIEWER)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "human_review")

    def test_pushback_while_waiting_for_merge_reaches_the_developer(self):
        office = self.office()
        iid = office.to_human_review()
        office.gitlab.approve(iid, REVIEWER)
        office.manager.tick()
        office.gitlab.person_comments(iid, MERGER)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "fixing_person")
        self.assertIn("the maintainer", office.ticket()["todo"]["message"])
        office.gitlab.developer_replies(iid)
        office.say("developer", "fixed")
        self.assertEqual(office.ticket()["state"], "waiting_merge")

    def test_a_closed_merge_request_goes_to_the_owner(self):
        office = self.office()
        iid = office.to_human_review()
        office.gitlab.mrs[iid]["state"] = "closed"
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "needs_owner")


class Owner(Case):
    def test_a_question_waits_for_the_owner_and_the_answer_goes_to_the_developer_not_to_jira(self):
        office = self.office()
        office.open_office()
        office.say("developer", "blocked", question="Keep the old variable?")
        self.assertEqual(office.ticket()["state"], "blocked")
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "blocked")

        self.assertTrue(office.manager.answer(KEY, "Keep it for now."))
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "working")
        self.assertIn("Keep it for now.", office.ticket()["todo"]["message"])
        self.assertEqual(office.jira.comments, [])

    def test_only_a_core_design_decision_is_written_on_jira(self):
        office = self.office()
        office.open_office()
        office.say("developer", "blocked", question="D1 cannot work. Read the run id from the scheduler instead?", design=True)
        self.assertTrue(office.ticket()["question"]["design"])
        self.assertTrue(office.manager.answer(KEY, "Yes, read it from the scheduler.", record=True))
        office.manager.tick()
        self.assertEqual(len(office.jira.comments), 1)
        self.assertIn("Core design decision", office.jira.comments[0][1])
        self.assertIn("Yes, read it from the scheduler.", office.jira.comments[0][1])

    def test_the_merge_request_link_is_the_one_thing_always_written_on_jira(self):
        office = self.office()
        iid = office.to_merge_request()
        self.assertEqual(office.jira.status(KEY), "Completed")
        self.assertEqual(len(office.jira.comments), 1)
        self.assertEqual(office.jira.comments[0][1], f"Merge request: {office.gitlab.mr(iid)['web_url']}")
        # Nothing more is written through the review, the approval and the merge.
        office.say("reviewer", "passed")
        office.gitlab.approve(iid, REVIEWER)
        office.manager.tick()
        office.gitlab.merge(iid)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "on_staging")
        self.assertEqual(len(office.jira.comments), 1)

    def test_too_many_review_rounds_stop_at_the_owner_and_can_be_resumed(self):
        office = self.office(agent_review__enabled=True)
        office.to_merge_request()
        for _ in range(office.cfg["limits"]["review_rounds"]):
            office.say("reviewer", "threads_posted", count=1)
            office.say("developer", "fixed")
        self.assertEqual(office.ticket()["state"], "needs_owner")

        self.assertTrue(office.manager.resume(KEY, note="One more round is fine."))
        self.assertEqual(office.ticket()["state"], "fixing")
        self.assertIn("One more round is fine.", office.ticket()["todo"]["message"])

    def test_a_failed_role_goes_to_the_owner(self):
        office = self.office()
        office.open_office()
        office.say("developer", "failed", note="the smoke run needs the VPN")
        self.assertEqual(office.ticket()["state"], "needs_owner")
        self.assertIn("VPN", office.ticket()["error"])

    def test_an_unexpected_report_is_set_aside(self):
        office = self.office()
        office.open_office()
        office.say("reviewer", "passed")
        self.assertEqual(office.ticket()["state"], "working")
        self.assertTrue(office.store.read()["events"][-1]["attention"])

    def test_answers_cover_only_the_offices_own_merge_requests(self):
        office = self.office()
        iid = office.to_human_review()
        office.gitlab.approve(iid, REVIEWER)
        office.manager.tick()
        text = answer_question("How many are with the maintainer?", office.store.read(), office.cfg)
        self.assertIn("1 of the office's merge requests are with the maintainer", text)


class NoReviewerDesk(Case):
    """The flow as it runs for now: developer, co-author, then straight to the human reviewer."""

    def test_the_merge_request_goes_straight_to_the_human_reviewer_and_on_to_staging(self):
        office = self.office()
        iid = office.to_merge_request()
        ticket = office.ticket()
        self.assertEqual((ticket["state"], ticket["todo"]), ("human_review", None))
        self.assertEqual(office.jira.status(KEY), "Completed")
        self.assertEqual(office.gitlab.mrs[iid]["reviewers"], [REVIEWER])
        self.assertIn("ready for review", json.dumps(office.gitlab.discussions(iid)))
        self.assertFalse(any(event["to"] == "reviewer" for event in office.store.read()["events"]))

        # A comment from the human reviewer goes to the developer, and the ticket comes back.
        office.gitlab.person_comments(iid, REVIEWER)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "fixing_person")
        self.assertEqual(office.ticket()["todo"]["role"], "developer")
        office.gitlab.developer_replies(iid)
        office.say("developer", "fixed")
        self.assertEqual(office.ticket()["state"], "human_review")

        office.gitlab.approve(iid, REVIEWER)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "waiting_merge")
        self.assertEqual(office.gitlab.mrs[iid]["assignees"], [MERGER])
        office.gitlab.merge(iid)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "on_staging")

    def test_the_next_ticket_starts_once_the_first_is_with_the_human_reviewer(self):
        office = self.office()
        office.open_office()
        office.jira.move("DEMO-102", "In Progress")
        office.manager.tick()
        self.assertNotIn("DEMO-102", office.store.read()["tickets"])
        office.say("developer", "built")
        office.say("coauthor", "clean")
        office.say("developer", "mr_open", mr=office.gitlab.open(KEY, "staging"))
        self.assertEqual(office.ticket()["state"], "human_review")
        office.manager.tick()
        self.assertEqual(office.ticket("DEMO-102")["state"], "working")

    def test_a_ticket_left_on_the_reviewers_desk_moves_to_the_human_reviewer(self):
        office = self.office(agent_review__enabled=True)
        iid = office.to_merge_request()
        office.gitlab.reviewer_posts(iid, 2)
        self.assertEqual(office.ticket()["state"], "in_review")
        office.cfg["agent_review"]["enabled"] = False
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "human_review")
        self.assertEqual(office.gitlab.mrs[iid]["reviewers"], [REVIEWER])
        told = [event["text"] for event in office.store.read()["events"] if event["attention"]]
        self.assertTrue(any("2 thread(s) the reviewer agent opened" in text for text in told), told)

    def test_the_coauthor_is_the_check_on_scope(self):
        office = self.office()
        office.open_office()
        office.say("developer", "built")
        office.say("coauthor", "findings", count=1, file="coauthor-1.md", drift=True)
        self.assertEqual(office.ticket()["state"], "working")
        office.say("developer", "built")
        # Still outside the ticket's scope after the last round: it does not go out for review.
        office.say("coauthor", "findings", count=1, file="coauthor-2.md", drift=True)
        self.assertEqual(office.ticket()["state"], "needs_owner")
        self.assertIn("does not match the ticket's scope", office.ticket()["error"])

    def test_other_findings_in_the_last_round_do_not_hold_the_merge_request_back(self):
        office = self.office()
        office.open_office()
        office.say("developer", "built")
        office.say("coauthor", "findings", count=2, file="coauthor-1.md")
        office.say("developer", "built")
        office.say("coauthor", "findings", count=1, file="coauthor-2.md")
        self.assertEqual(office.ticket()["state"], "opening_mr")


class ReviewerAgentOnly(Case):
    """The flow this repository ships with: the reviewer agent reviews, and no person does before the merge."""

    def office(self, **overrides):
        return super().office(human_review__enabled=False, agent_review__enabled=True, **overrides)

    def test_a_merge_request_the_reviewer_agent_passed_goes_to_the_maintainer(self):
        office = self.office()
        iid = office.to_merge_request()
        self.assertEqual(office.ticket()["state"], "in_review")
        office.say("reviewer", "passed")
        self.assertEqual(office.ticket()["waiting_on"], "checks")
        self.assertEqual(office.gitlab.mrs[iid]["reviewers"], [])
        app = server.App.__new__(server.App)
        app.cfg, app.mode, app.stepping, app.store, app.jira = office.cfg, "manual", True, office.store, office.jira
        card = next(t for t in app.snapshot()["tickets"] if t["key"] == KEY)
        self.assertEqual((card["lane"], card["label"]), ("merger", "Waiting for checks"))
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "waiting_merge")
        self.assertEqual(office.gitlab.mrs[iid]["assignees"], [MERGER])
        self.assertIn("approved by the reviewer agent", json.dumps(office.gitlab.discussions(iid)))
        office.gitlab.merge(iid)
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "on_staging")

    def test_the_default_settings_are_this_flow(self):
        self.assertEqual((config.DEFAULTS["agent_review"]["enabled"], config.DEFAULTS["human_review"]["enabled"]), (True, False))


class StartingAgain(Case):
    def test_a_closed_merge_request_from_an_earlier_try_does_not_block_a_fresh_start(self):
        office = self.office()
        with mock.patch.object(office.gitlab, "mr_for_branch", return_value={"iid": 9, "state": "closed"}):
            office.open_office()
        self.assertEqual(office.ticket()["state"], "working")

    def test_an_open_merge_request_or_a_branch_the_office_did_not_make_still_stops_it(self):
        for name, value in (("mr_for_branch", {"iid": 9, "state": "opened"}), ("branch_exists", True)):
            office = self.office()
            with mock.patch.object(office.gitlab, name, return_value=value):
                office.open_office()
            self.assertEqual(office.ticket()["state"], "needs_owner", name)
            self.assertIn("already exists", office.ticket()["error"])


class Forget(Case):
    def test_a_forgotten_ticket_is_taken_fresh_on_the_next_start(self):
        office = self.office()
        office.open_office()
        office.say("developer", "blocked", question="Which way?")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OFFICE_", "JIRA_", "GITLAB_"))}
        env.update(OFFICE_STATE_DIR=office.tmp.name, OFFICE_ENV_FILE=str(Path(office.tmp.name) / "absent"))
        done = subprocess.run([sys.executable, str(config.HOME / "officectl.py"), "forget", KEY], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        state = office.store.read()
        self.assertNotIn(KEY, state["tickets"])
        self.assertFalse(any(report["ticket"] == KEY for report in state["reports"]))
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "working")

    def test_a_role_cannot_forget_a_ticket(self):
        env = dict(os.environ, OFFICE_ROLE="developer")
        done = subprocess.run([sys.executable, str(config.HOME / "officectl.py"), "forget", KEY], env=env, capture_output=True, text=True, check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("does not run `forget`", done.stderr)


class Pickup(Case):
    def test_start_takes_a_ticket_that_is_already_in_progress(self):
        office = self.office()
        office.jira.move(KEY, "In Progress")
        office.jira.tickets[KEY]["moved"] = datetime.now(timezone.utc) - timedelta(hours=2)
        office.manager.tick()
        # Nothing is taken while the office is closed.
        self.assertEqual(office.store.read()["tickets"], {})
        office.manager.start()
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "working")

    def test_with_the_other_setting_a_ticket_in_progress_before_start_is_left_alone(self):
        office = self.office(jira__pickup="after_start")
        office.jira.move(KEY, "In Progress")
        office.jira.tickets[KEY]["moved"] = datetime.now(timezone.utc) - timedelta(hours=2)
        office.manager.start()
        office.manager.tick()
        self.assertEqual(office.store.read()["tickets"], {})

    def test_one_ticket_at_a_time_until_it_is_with_a_person(self):
        office = self.office(agent_review__enabled=True)
        office.to_merge_request()
        office.jira.move("DEMO-102", "In Progress")
        office.manager.tick()
        self.assertNotIn("DEMO-102", office.store.read()["tickets"])
        office.say("reviewer", "passed")
        office.manager.tick()
        self.assertIn("DEMO-102", office.store.read()["tickets"])

    def test_nothing_is_picked_up_while_the_office_is_closed(self):
        office = self.office()
        office.jira.move(KEY, "In Progress")
        office.manager.tick()
        self.assertEqual(office.store.read()["tickets"], {})

    def test_an_existing_branch_is_not_taken_over(self):
        office = self.office()
        office.gitlab.branch_exists = lambda name: True
        office.open_office()
        self.assertEqual(office.ticket()["state"], "needs_owner")


class ByRole(Case):
    """The human reviewer is named by the role everywhere, and the account is shown nowhere."""

    def test_the_request_for_review_names_no_one(self):
        office = self.office()
        iid = office.to_human_review()
        self.assertEqual(office.gitlab.mrs[iid]["reviewers"], [REVIEWER])
        said = json.dumps([office.gitlab.discussions(iid), office.store.read()["events"][-1]["text"]])
        self.assertIn("It is with the human reviewer", said)
        self.assertNotIn(REVIEWER, said)

    def test_the_page_and_the_context_show_the_role_not_the_account(self):
        office = self.office()
        iid = office.to_human_review()
        office.gitlab.approve(iid, REVIEWER)
        office.manager.tick()
        app = server.App.__new__(server.App)
        app.cfg, app.mode, app.stepping, app.store, app.jira = office.cfg, "manual", True, office.store, office.jira
        office.jira.move("DEMO-103", "In Progress")
        # Before Start the page says what Start would take, and whether the ticket check would let it through.
        self.assertEqual([(t["key"], bool(t["faults"])) for t in app.snapshot()["upcoming"]], [("DEMO-103", True)])
        page = json.dumps(app.snapshot())
        self.assertNotIn(REVIEWER, page)
        self.assertIn('"approved_by": ["human_reviewer"]', page)
        told = "\n".join(part["text"] for part in context.build(office.cfg, office.store.read(), KEY, office.jira))
        self.assertNotIn(REVIEWER, told)
        self.assertIn("Approved by: the human reviewer", told)
        self.assertIn("With the human reviewer", answer_question("what is with the human reviewer", office.store.read(), office.cfg))

    def test_with_no_human_reviewer_set_the_ticket_stops_at_the_owner(self):
        office = self.office(people__human_reviewer="")
        iid = office.to_merge_request()
        office.cfg["people"]["human_reviewer"] = ""
        office.say("reviewer", "passed")
        self.assertEqual(office.ticket()["state"], "needs_owner")
        self.assertIn("no human reviewer is set", office.ticket()["error"])
        self.assertEqual(office.gitlab.mrs[iid]["reviewers"], [])


class Safety(Case):
    def test_a_preview_changes_nothing(self):
        office = self.office(agent_review__enabled=True)
        iid = office.to_merge_request()
        office.store.update(lambda state: add_report(state, KEY, "reviewer", "passed"))
        before = office.store.read()
        log = office.manager.tick(dry=True)
        self.assertTrue(any("Set the human reviewer as reviewer" in line for line in log))
        self.assertEqual(office.store.read(), before)
        self.assertEqual(office.gitlab.mrs[iid]["reviewers"], [])

    def test_a_report_survives_gitlab_being_unreachable(self):
        office = self.office(agent_review__enabled=True)
        office.open_office()
        office.say("developer", "built")
        office.say("coauthor", "clean")
        iid = office.gitlab.open(KEY, "staging")
        real = office.gitlab.mr

        def down(_iid):
            raise ApiError(0, "timed out")

        office.gitlab.mr = down
        office.say("developer", "mr_open", mr=iid)
        self.assertEqual(office.ticket()["state"], "opening_mr")
        office.gitlab.mr = real
        office.manager.tick()
        self.assertEqual(office.ticket()["state"], "in_review")

    def test_a_merge_request_into_the_wrong_branch_is_refused(self):
        office = self.office()
        office.open_office()
        office.say("developer", "built")
        office.say("coauthor", "clean")
        iid = office.gitlab.open(KEY, "main")
        office.say("developer", "mr_open", mr=iid)
        self.assertEqual(office.ticket()["state"], "needs_owner")


class Pieces(Case):
    def test_threads_are_sorted_by_who_opened_them(self):
        gitlab = FakeGitLab(OWNER)
        iid = gitlab.open(KEY, "staging")
        gitlab.reviewer_posts(iid, 2)
        gitlab.person_comments(iid, REVIEWER)
        gitlab.comment(iid, stamp("manager", "Please review."))
        gitlab.add_thread(iid, MERGER, "Please rebase.", resolvable=False)
        facts = classify(gitlab.discussions(iid))
        self.assertEqual((facts["agent_total"], facts["agent_open"]), (2, 2))
        self.assertEqual([t["author"] for t in facts["person_threads"]], [REVIEWER])
        self.assertEqual([t["author"] for t in facts["new_comments"]], [MERGER])
        self.assertEqual(classify(gitlab.discussions(iid), facts["last_note_id"])["new_comments"], [])

    def test_a_title_with_a_ticket_number_is_caught(self):
        self.assertTrue(TICKET_ID.search("PROJ-104: add the quarter"))
        self.assertTrue(TICKET_ID.search("add the quarter (PROJ-104 + SCRUM-1)"))
        self.assertFalse(TICKET_ID.search("add the quarter and the admin run to the report"))

    def test_tokens_come_from_the_owners_env_file_and_the_environment_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "env"
            path.write_text("# tokens\nJIRA_EMAIL=me@example.com\nexport JIRA_TOKEN='from-file'\nGITLAB_TOKEN=\"glfile\"\n", encoding="utf-8")
            clean = {k: v for k, v in os.environ.items() if k not in ("JIRA_EMAIL", "JIRA_TOKEN", "GITLAB_TOKEN")}
            with mock.patch.dict(os.environ, {**clean, "OFFICE_ENV_FILE": str(path)}, clear=True):
                self.assertEqual(config.tokens(), {"jira_email": "me@example.com", "jira_token": "from-file", "gitlab_token": "glfile"})
                self.assertEqual(config.missing_tokens(), [])
            with mock.patch.dict(os.environ, {**clean, "OFFICE_ENV_FILE": str(path), "JIRA_TOKEN": "from-env"}, clear=True):
                self.assertEqual(config.tokens()["jira_token"], "from-env")
            with mock.patch.dict(os.environ, {**clean, "OFFICE_ENV_FILE": str(Path(folder) / "absent")}, clear=True):
                self.assertEqual(config.missing_tokens(), ["JIRA_EMAIL", "JIRA_TOKEN", "GITLAB_TOKEN"])

    def test_run_passes_the_other_credentials_and_holds_the_tokens_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "env"
            path.write_text("GITLAB_TOKEN=secret\nJIRA_TOKEN=secret\nDB_USER='someone'\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in ("GITLAB_TOKEN", "JIRA_TOKEN", "DB_USER")}
            env.update(OFFICE_ENV_FILE=str(path), OFFICE_STATE_DIR=folder, OFFICE_ROLE="developer")
            probe = "import os; print(os.environ.get('DB_USER'), os.environ.get('GITLAB_TOKEN'), os.environ.get('JIRA_TOKEN'))"
            cmd = [sys.executable, str(config.HOME / "officectl.py"), "run", "--", sys.executable, "-c", probe]
            done = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(done.stdout.strip(), "someone None None")

    def test_agents_write_only_on_the_offices_own_merge_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            def seed(state):
                for key, iid in (("PROJ-1", 7), ("PROJ-2", 8)):
                    state["tickets"][key] = dict(new_ticket(key), mr_iid=iid)
            store.update(seed)
            base = {k: v for k, v in os.environ.items() if not k.startswith("OFFICE_")}
            base.update(OFFICE_STATE_DIR=folder, OFFICE_DRY="1", OFFICE_ROLE="developer")
            def comment(iid, **extra):
                cmd = [sys.executable, str(config.HOME / "officectl.py"), "comment", "--mr", str(iid), "--body", "hello"]
                return subprocess.run(cmd, env={**base, **extra}, capture_output=True, text=True, check=False)
            self.assertIn("would comment on !7", comment(7).stdout)
            self.assertIn("not one of the office", comment(999).stderr)
            self.assertIn("would comment on !7", comment(7, OFFICE_TICKET="PROJ-1").stdout)
            self.assertIn("not the merge request for PROJ-1", comment(8, OFFICE_TICKET="PROJ-1").stderr)

    def test_jira_text_round_trips(self):
        text = "Decision recorded.\n\nQuestion: A?\nAnswer: B."
        self.assertEqual(flatten(to_adf(text)).strip(), "Decision recorded.\nQuestion: A?\nAnswer: B.")

    def test_the_cli_call_resumes_a_known_session_and_starts_a_fresh_one_when_asked(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg["state_dir"] = folder.name
        launcher = ClaudeLauncher(cfg, None)
        ticket = new_ticket(KEY, "A ticket")
        ticket["sessions"]["developer"] = "known-session"
        cmd, session, _ = launcher.command(ticket, {"role": "developer", "message": "Carry on.", "fresh": False})
        self.assertEqual(session, "known-session")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "known-session")
        cmd, session, _ = launcher.command(ticket, {"role": "reviewer", "message": "Review.", "fresh": True})
        self.assertIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        settings = Path(cmd[cmd.index("--settings") + 1])
        self.assertEqual(settings.parent, Path(cfg["state_dir"]))
        self.assertIn(str(config.HOME / "guards" / "guard.py"), settings.read_text(encoding="utf-8"))
        self.assertNotIn("--resume", cmd)

    def test_every_session_runs_behind_the_guard_and_gets_no_credential(self):
        with tempfile.TemporaryDirectory() as folder:
            fake, seen = Path(folder) / "claude", Path(folder) / "seen.txt"
            fake.write_text(f'#!/bin/sh\nenv > {seen}\necho "$@" >> {seen}\necho \'{{"type": "result", "total_cost_usd": 0.5}}\'\n', encoding="utf-8")
            fake.chmod(0o755)
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg["state_dir"], cfg["claude"]["bin"] = folder, str(fake)
            store = Store(folder)
            launcher = ClaudeLauncher(cfg, store)
            todo = {"role": "developer", "message": "Start.", "fresh": True}

            def begin(state, worktree):
                state["tickets"][KEY] = new_ticket(KEY, "A ticket")
                state["tickets"][KEY]["worktree"] = worktree
                return launcher.launch(state, state["tickets"][KEY], todo)

            # No working copy of its own: nothing is started, and the manager hears why.
            self.assertFalse(store.update(lambda state: begin(state, "")))
            self.assertIn("No session was started", store.read()["reports"][-1]["note"])

            with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "secret-token", "DB_PASSWORD": "secret-pass"}):
                self.assertTrue(store.update(lambda state: begin(state, folder)))
                launcher._thread.join(timeout=20)
            told = seen.read_text(encoding="utf-8")
            self.assertIn("--permission-mode dontAsk", told)
            self.assertIn("session-settings.json", told)
            self.assertIn(f"OFFICE_WORKTREE={folder}", told)
            self.assertIn("OFFICE_ROLE=developer", told)
            self.assertNotIn("secret-", told)
            # It ended without reporting, so the ticket does not sit there silently.
            last = store.read()["reports"][-1]
            self.assertEqual((last["status"], store.read()["tickets"][KEY]["cost_usd"]), ("failed", 0.5))

    def test_auto_mode_waits_for_start_and_moves_when_something_is_reported(self):
        office = self.office()
        office.manager.start()
        app = server.App.__new__(server.App)
        app.cfg, app.mode, app.stepping, app.store, app.manager, app.pace = office.cfg, "auto", False, office.store, office.manager, 60.0
        office.store.update(app._reset)
        self.assertFalse(office.store.read()["running"])
        seen = office.store.read()["seq"]
        self.assertFalse(app._due(seen, time.monotonic()))
        office.manager.start()
        self.assertTrue(app._due(seen, time.monotonic()))

    def test_on_start_the_ticket_in_progress_is_taken_and_one_session_follows_another(self):
        """The whole automatic chain, with a stand-in for the Claude CLI that only reports."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            origin, repo, fake = root / "origin.git", root / "repo", root / "claude"

            def git(*args, cwd=None):
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True, capture_output=True)

            git("init", "-q", "--bare", "-b", "staging", str(origin))
            git("clone", "-q", str(origin), str(repo))
            (repo / "README.md").write_text("x\n", encoding="utf-8")
            git("add", "-A", cwd=repo)
            git("commit", "-q", "-m", "x", cwd=repo)
            git("push", "-q", "origin", "HEAD:staging", cwd=repo)
            fake.write_text(
                '#!/bin/sh\ncase "$OFFICE_ROLE" in developer) said=built ;; coauthor) said=clean ;; esac\n'
                f'python3 {config.HOME}/officectl.py report --ticket "$OFFICE_TICKET" --status "$said" > /dev/null\n'
                'echo \'{"type": "result", "total_cost_usd": 0.1}\'\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg["state_dir"], cfg["claude"]["bin"] = str(root / "state"), str(fake)
            cfg["repo"] = {"path": str(repo), "worktrees": str(root / "worktrees")}
            store, jira = Store(cfg["state_dir"]), FakeJira()
            launcher = ClaudeLauncher(cfg, store)
            manager = Manager(cfg, store, jira, FakeGitLab(OWNER), launcher)
            jira.move(KEY, "In Progress")  # before Start, as on the owner's board

            with mock.patch.dict(os.environ, {"OFFICE_STATE_DIR": cfg["state_dir"]}):
                manager.start()
                manager.tick()
                launcher._thread.join(timeout=30)  # the developer's session
                manager.tick()
                launcher._thread.join(timeout=30)  # the co-author's session
                manager.stop()
                manager.tick()

            ticket = store.read()["tickets"][KEY]
            self.assertEqual(ticket["state"], "opening_mr")
            self.assertEqual(ticket["todo"]["role"], "developer")
            self.assertEqual(set(ticket["sessions"]), {"developer", "coauthor"})
            self.assertEqual(ticket["worktree"], str(root / "worktrees" / KEY))
            head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ticket["worktree"], capture_output=True, text=True, check=True)
            self.assertEqual(head.stdout.strip(), KEY)
            self.assertEqual(ticket["cost_usd"], 0.2)

    def test_a_ticket_started_as_it_is_skips_the_check_and_tells_the_developer(self):
        office = self.office()
        office.jira.tickets["DEMO-103"]["status"] = "In Progress"
        office.manager.add("DEMO-103", unplanned=True)
        office.manager.tick()
        # Nothing is started while the office is closed.
        self.assertEqual(office.ticket("DEMO-103")["state"], "listed")
        office.manager.start()
        office.manager.tick()
        ticket = office.ticket("DEMO-103")
        self.assertEqual(ticket["state"], "working")
        self.assertIn("started as it is", ticket["todo"]["message"])
        self.assertEqual(office.jira.comments, [])

    def test_a_session_that_ends_without_reporting_is_flagged(self):
        state = {"active": {"ticket": KEY}, "tickets": {KEY: new_ticket(KEY)}, "reports": [], "seq": 5}
        _finish(state, KEY, "developer", since=5, code=1, cost=0.25)
        self.assertEqual(state["reports"][0]["status"], "failed")
        self.assertIsNone(state["active"])
        self.assertEqual(state["tickets"][KEY]["cost_usd"], 0.25)

    def test_stream_events_become_one_line(self):
        event = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest\n-q"}}]}}
        self.assertEqual(describe(event), "Bash: uv run pytest")
        self.assertEqual(describe({"type": "result", "is_error": False}), "Finished")


if __name__ == "__main__":
    unittest.main()
