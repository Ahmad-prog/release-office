"""A small JSON-over-HTTPS helper shared by the Jira and GitLab clients."""

import json
import urllib.error
import urllib.parse
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def call(method, url, headers, params=None, body=None, timeout=40):
    """Returns ``(parsed body, response headers)``. Raises ApiError on a failed request."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    headers = dict(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return (json.loads(raw) if raw.strip() else None), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode("utf-8", "replace")[:300]) from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, str(exc.reason)) from exc
