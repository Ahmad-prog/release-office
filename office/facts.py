"""What a merge request looks like right now, and whose threads are whose.

Every role works on the owner's token for now, so GitLab shows one author for all of them.
A hidden marker in each note says which role wrote it, and a visible label says the same
to the people reading the merge request.
"""

MARK = {
    "reviewer": "<!-- release-office:reviewer -->",
    "developer": "<!-- release-office:developer -->",
    "manager": "<!-- release-office:manager -->",
}
LABEL = {
    "reviewer": "**Reviewer agent · Release Office**",
    "developer": "**Developer agent · Release Office**",
    "manager": "**Manager · Release Office**",
}


def stamp(role, body):
    """Label a note so people can see an agent wrote it, and so the office can find it again."""
    return f"{LABEL[role]}\n\n{body.strip()}\n\n{MARK[role]}"


def role_of(body):
    for role, mark in MARK.items():
        if mark in (body or ""):
            return role
    return None


def describe_threads(discussions):
    """One plain record per discussion, in the order GitLab returns them."""
    threads = []
    for discussion in discussions:
        notes = [note for note in discussion.get("notes") or [] if not note.get("system")]
        if not notes:
            continue
        first, last = notes[0], notes[-1]
        position = first.get("position") or {}
        threads.append(
            {
                "id": discussion["id"],
                "resolvable": bool(first.get("resolvable")),
                "resolved": bool(first.get("resolved")),
                "author": first["author"]["username"],
                "opened_by": role_of(first.get("body")) or "person",
                "last_by": role_of(last.get("body")) or "person",
                "last_author": last["author"]["username"],
                "last_note_id": max(note["id"] for note in notes),
                "path": position.get("new_path") or position.get("old_path") or "",
                "line": position.get("new_line") or position.get("old_line") or "",
                "notes": [
                    {"id": note["id"], "author": note["author"]["username"], "body": note.get("body", "")}
                    for note in notes
                ],
            }
        )
    return threads


def classify(discussions, seen_note_id=0):
    """Split a merge request's discussions into the reviewer agent's threads and people's."""
    threads = describe_threads(discussions)
    agent = [t for t in threads if t["resolvable"] and t["opened_by"] == "reviewer"]
    # A person's thread needs the developer when the person spoke last.
    waiting = [
        t
        for t in threads
        if t["resolvable"] and t["opened_by"] == "person" and not t["resolved"] and t["last_by"] != "developer"
    ]
    # A plain comment needs the developer once, when it is newer than what was last handled.
    comments = [
        t for t in threads if not t["resolvable"] and t["opened_by"] == "person" and t["last_note_id"] > seen_note_id
    ]
    return {
        "agent_total": len(agent),
        "agent_open": sum(1 for t in agent if not t["resolved"]),
        "person_threads": waiting,
        "new_comments": comments,
        "people_open": sum(1 for t in threads if t["resolvable"] and t["opened_by"] == "person" and not t["resolved"]),
        "last_note_id": max((t["last_note_id"] for t in threads), default=0),
    }


def mr_facts(gitlab, iid, seen_note_id=0):
    mr = gitlab.mr(iid)
    pipeline = mr.get("head_pipeline") or {}
    facts = classify(gitlab.discussions(iid), seen_note_id)
    facts.update(
        {
            "state": mr.get("state"),
            "draft": bool(mr.get("draft") or mr.get("work_in_progress")),
            "sha": mr.get("sha") or "",
            "pipeline": pipeline.get("status") or "",
            # A green pipeline only counts when it ran on the commit that would be merged.
            "pipeline_on_head": bool(pipeline) and pipeline.get("sha") == mr.get("sha"),
            "conflicts": bool(mr.get("has_conflicts")),
            "threads_resolved": bool(mr.get("blocking_discussions_resolved", True)),
            "approved_by": gitlab.approvals(iid),
            "web_url": mr.get("web_url") or "",
            "source_branch": mr.get("source_branch") or "",
            "target_branch": mr.get("target_branch") or "",
        }
    )
    return facts
