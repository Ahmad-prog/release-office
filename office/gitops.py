"""Git helpers. The token reaches git through an askpass script, never through a URL or argv."""

import os
import stat
import subprocess
from pathlib import Path

PROTECTED = {"main", "master", "staging"}
ASKPASS = """#!/bin/sh
case "$1" in
  *sername*) echo oauth2 ;;
  *) echo "$OFFICE_GIT_TOKEN" ;;
esac
"""


class GitError(RuntimeError):
    pass


def _askpass(directory):
    path = Path(directory) / "git-askpass.sh"
    if not path.exists():
        path.write_text(ASKPASS, encoding="utf-8")
        path.chmod(stat.S_IRWXU)
    return str(path)


def git(args, cwd, token="", helper_dir="", check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    cmd = [os.environ.get("OFFICE_GIT", "git")]
    if token:
        env.update(GIT_ASKPASS=_askpass(helper_dir), OFFICE_GIT_TOKEN=token)
        cmd += ["-c", "credential.helper="]
    done = subprocess.run(cmd + list(args), cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise GitError((done.stderr or done.stdout).strip()[:400])
    return done


def current_branch(cwd):
    return git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()


def fetch(cwd, branch, token, helper_dir):
    git(["fetch", "origin", branch], cwd, token, helper_dir)


def add_worktree(repo, path, branch, start_point):
    """A fresh working copy on a new branch, so a ticket never rides on someone's work in progress."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(path), start_point], repo)


def push(cwd, branch, token, helper_dir, force_with_lease=False):
    """Push the current branch to ``branch``. Refuses the release branches and a diverged remote."""
    if branch in PROTECTED:
        raise GitError(f"refusing to push to {branch}")
    here = current_branch(cwd)
    if here != branch:
        raise GitError(f"this working copy is on {here}, not {branch}")
    listed = git(["ls-remote", "--heads", "origin", branch], cwd, token, helper_dir).stdout.split()
    remote_sha = listed[0] if listed else ""
    args = ["push", "origin", f"HEAD:refs/heads/{branch}"]
    if remote_sha:
        git(["fetch", "origin", branch], cwd, token, helper_dir)
        ahead = git(["merge-base", "--is-ancestor", remote_sha, "HEAD"], cwd, check=False).returncode == 0
        if not ahead:
            if not force_with_lease:
                raise GitError(
                    "the remote branch has commits this working copy does not. Rebase onto it, "
                    "or pass --force-with-lease if you rebased the branch yourself."
                )
            args.insert(1, f"--force-with-lease={branch}:{remote_sha}")
    done = git(args, cwd, token, helper_dir)
    return (done.stderr or done.stdout).strip()
