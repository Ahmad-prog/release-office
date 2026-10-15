"""GitLab client: merge requests, their threads, and who they are assigned to."""

import urllib.parse

from office.api import ApiError, call


class GitLab:
    def __init__(self, base_url, project_id, token):
        self.api = f"{base_url.rstrip('/')}/api/v4"
        self.project = f"{self.api}/projects/{project_id}"
        self.headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        self._user_ids = {}

    def _call(self, method, url, params=None, body=None):
        return call(method, url, self.headers, params, body)[0]

    def _paged(self, url, params=None):
        items, page = [], 1
        while True:
            data, headers = call("GET", url, self.headers, dict(params or {}, per_page=100, page=page))
            items += data or []
            following = headers.get("X-Next-Page") or headers.get("x-next-page")
            if not following:
                return items
            page = int(following)

    def user_id(self, username):
        if username not in self._user_ids:
            found = self._call("GET", f"{self.api}/users", {"username": username})
            if not found:
                raise ApiError(404, f"no GitLab user named {username}")
            self._user_ids[username] = found[0]["id"]
        return self._user_ids[username]

    def branch_exists(self, name):
        try:
            self._call("GET", f"{self.project}/repository/branches/{urllib.parse.quote(name, safe='')}")
            return True
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise

    # ---- merge requests

    def mr(self, iid):
        return self._call("GET", f"{self.project}/merge_requests/{iid}")

    def mr_for_branch(self, branch):
        found = self._call(
            "GET",
            f"{self.project}/merge_requests",
            {"source_branch": branch, "order_by": "created_at", "sort": "desc", "per_page": 1},
        )
        return found[0] if found else None

    def create_mr(self, source, target, title, description):
        body = {
            "source_branch": source,
            "target_branch": target,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        }
        return self._call("POST", f"{self.project}/merge_requests", body=body)

    def approvals(self, iid):
        data = self._call("GET", f"{self.project}/merge_requests/{iid}/approvals")
        return [item["user"]["username"] for item in (data or {}).get("approved_by") or []]

    def set_reviewers(self, iid, usernames):
        ids = [self.user_id(name) for name in usernames]
        self._call("PUT", f"{self.project}/merge_requests/{iid}", body={"reviewer_ids": ids})

    def set_assignees(self, iid, usernames):
        ids = [self.user_id(name) for name in usernames]
        self._call("PUT", f"{self.project}/merge_requests/{iid}", body={"assignee_ids": ids})

    # ---- threads and comments

    def discussions(self, iid):
        return self._paged(f"{self.project}/merge_requests/{iid}/discussions")

    def position(self, iid, path, line):
        """Where an inline thread sits: a line that exists at the merge request's head."""
        refs = self.mr(iid)["diff_refs"]
        return {
            "base_sha": refs["base_sha"],
            "start_sha": refs["start_sha"],
            "head_sha": refs["head_sha"],
            "position_type": "text",
            "old_path": path,
            "new_path": path,
            "new_line": int(line),
        }

    def post_thread(self, iid, body, position=None):
        payload = {"body": body}
        if position:
            payload["position"] = position
        return self._call("POST", f"{self.project}/merge_requests/{iid}/discussions", body=payload)

    def reply(self, iid, discussion_id, body):
        url = f"{self.project}/merge_requests/{iid}/discussions/{discussion_id}/notes"
        return self._call("POST", url, body={"body": body})

    def resolve(self, iid, discussion_id, resolved=True):
        url = f"{self.project}/merge_requests/{iid}/discussions/{discussion_id}"
        return self._call("PUT", url, {"resolved": "true" if resolved else "false"})

    def comment(self, iid, body):
        return self._call("POST", f"{self.project}/merge_requests/{iid}/notes", body={"body": body})
