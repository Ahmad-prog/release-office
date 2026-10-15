"""What waits on the owner: the context he can open, the follow-up he can ask, the answer he gives."""

import unittest

from office import context
from office.config import ticket_dir
from tests.test_office import KEY, Case

QUESTION = "Keep the old variable, or remove it now?"


class Needs(Case):
    def blocked(self):
        office = self.office()
        office.open_office()
        notes = ticket_dir(office.cfg, KEY)
        (notes / "brief.md").write_text("# Brief\n- R1 as on the ticket.", encoding="utf-8")
        (notes / "question.md").write_text("## What I found\nTwo other jobs still read it.", encoding="utf-8")
        office.say("developer", "blocked", question=QUESTION, file="question.md")
        return office

    def parts(self, office):
        return {part["title"]: part["text"] for part in context.build(office.cfg, office.store.read(), KEY, office.jira)}

    def test_the_owner_gets_the_whole_context_the_developer_recorded(self):
        office = self.blocked()
        parts = self.parts(office)
        self.assertEqual(parts["The developer's question"], QUESTION)
        self.assertIn("Two other jobs still read it.", parts["What the developer wrote about it"])
        self.assertIn("R1 as on the ticket.", parts["The developer's brief"])
        self.assertIn("**R1.**", parts["On the ticket: Requirements"])
        self.assertIn("It stopped while: building.", parts["Where the work stands"])
        self.assertIn("developer to manager", parts["Messages about this ticket"])
        self.assertIn("question.md", parts["Files in the notes folder"])

    def test_a_follow_up_goes_to_the_developer_and_the_reply_comes_back(self):
        office = self.blocked()
        self.assertTrue(office.manager.follow_up(KEY, "Which two jobs?"))
        office.manager.tick()
        ticket = office.ticket()
        self.assertEqual(ticket["state"], "working")
        self.assertIn("Which two jobs?", ticket["todo"]["message"])
        self.assertIn(QUESTION, ticket["todo"]["message"])
        self.assertEqual(office.jira.comments, [])

        office.say("developer", "blocked", question=QUESTION, file="question.md", note="Added the two job names.")
        ticket = office.ticket()
        self.assertEqual(ticket["state"], "blocked")
        self.assertEqual([item["from"] for item in ticket["exchange"]], ["developer", "owner", "developer"])
        self.assertEqual(ticket["exchange"][-1]["text"], "Added the two job names.")
        self.assertIn("Owner: Which two jobs?", self.parts(office)["What has been said so far"])

    def test_the_resolution_goes_to_the_manager_then_the_developer_and_stays_off_jira(self):
        office = self.blocked()
        office.manager.follow_up(KEY, "Which two jobs?")
        office.manager.tick()
        office.say("developer", "blocked", question=QUESTION, file="question.md", note="Added them.")
        self.assertTrue(office.manager.answer(KEY, "Keep it. A follow-up ticket removes it."))
        office.manager.tick()
        ticket = office.ticket()
        self.assertEqual(ticket["state"], "working")
        self.assertIn("Keep it. A follow-up ticket removes it.", ticket["todo"]["message"])
        self.assertEqual(office.jira.comments, [])
        # The answer is kept in the ticket's notes, where the developer and the reviewer read it.
        kept = (ticket_dir(office.cfg, KEY) / "decisions.md").read_text(encoding="utf-8")
        self.assertIn("Keep it. A follow-up ticket removes it.", kept)
        self.assertEqual(ticket["exchange"], [])

    def test_a_follow_up_needs_a_waiting_question(self):
        office = self.office()
        office.open_office()
        self.assertFalse(office.manager.follow_up(KEY, "Anything?"))
        office.say("developer", "blocked", question=QUESTION)
        self.assertFalse(office.manager.follow_up(KEY, "  "))

    def test_a_stopped_ticket_says_why(self):
        office = self.office()
        office.open_office()
        office.say("developer", "failed", note="the smoke run needs the VPN")
        parts = self.parts(office)
        self.assertIn("VPN", parts["Why the office stopped"])
        self.assertIn("It stopped while: building.", parts["Where the work stands"])

    def test_only_the_tickets_own_notes_are_read(self):
        office = self.blocked()
        notes = ticket_dir(office.cfg, KEY)
        (notes.parent / "secret.txt").write_text("not for this ticket", encoding="utf-8")
        for name in ("../secret.txt", str(notes.parent / "secret.txt"), "/etc/hostname", "missing.md", ""):
            self.assertEqual(context.read_note(notes, name), "", name)
        self.assertIn("Two other jobs", context.read_note(notes, "question.md"))


if __name__ == "__main__":
    unittest.main()
