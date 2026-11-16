"""Create or refresh the orphan ``public`` branch from the current HEAD tree (design §11.1.1).

The public repository gets a fresh, single-commit history: ``main`` (whose history
starts from a local-only baseline) is never pushed. This script

1. checks the HEAD tree for paths that must never be published (business plugins,
   credentials, private enrollment files, local configs);
2. writes one commit with exactly that tree, authored by the public identity, and
   points ``refs/heads/public`` at it. The first run creates a parentless (orphan)
   commit; later runs add a child of the existing ``public`` tip, so ``public`` only
   ever moves forward: refreshing it never needs a force push, and ``v*`` tags made on
   earlier ``public`` commits stay inside its history (the pre-push hook keeps accepting
   them). ``main``'s history never enters ``public``. If HEAD's tree equals the tip's
   tree nothing is written. The working tree, the index and the current branch are not
   touched. The commit date is HEAD's, so re-running on the same HEAD yields the same
   commit;
3. installs a ``pre-push`` hook in this worktree's git dir that refuses every ref
   except ``refs/heads/public`` (and ``v*`` tags that point into ``public``'s history);
4. when a remote named ``origin`` exists: sets ``remote.origin.push =
   refs/heads/public:refs/heads/main`` and activates the hook for this worktree
   (``extensions.worktreeConfig`` + ``git config --worktree core.hooksPath``). Without a
   remote nothing can be pushed, so the shared repository config is left alone;
   ``--activate-hook`` forces activation anyway.

It never pushes and never adds a remote.

Usage::

    python scripts/publish/make_public_branch.py [--branch public] [--dry-run] [--activate-hook]
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTHOR_NAME = "muxigame"
AUTHOR_EMAIL = "76636667+muxigame@users.noreply.github.com"
PUSH_REFSPEC = "refs/heads/public:refs/heads/main"
HOOK_MARKER = "agent-nexus public-branch guard"

#: Paths that must never be in the public tree (globs on the repository path).
FORBIDDEN = (
    "builtins/chrome-devtools-mcp/*", "builtins/jubian-go-mcp/*", "builtins/ones-mcp/*",
    "builtins/payment-mcp/*", "builtins/unity-mcp/*", "builtins/wechat-mcp/*",
    "builtins/codex-mcp/logs/*", "*/logs/*oauth*", "*oauth-store*", "*oauth-password*",
    "secrets/*", "*/secrets/*", ".private/*", "*/.private/*", "*enrollment*.json", "*endpoint-allow",
    "mcp-manager.json", "*/mcp-manager.json", "mcp-manager.json.*", "agent-nexus.json", "*/agent-nexus.json",
    ".mcp-manager/*", "credentials-vault/*", ".extracted/*", "*.env", "*.pem", "*.key", "*.pfx", "*.p12",
    "*.sqlite3", "*.token", "*apikey*", "*api-key*", "*api_key*", "id_rsa*", "id_ed25519*",
)
ALLOWED = ("*.env.example",)

HOOK = """#!/bin/sh
# {marker}: only the public branch (and v* tags inside its history) may be pushed.
# Installed by scripts/publish/make_public_branch.py; main and legacy are local-only.
zero=0000000000000000000000000000000000000000
status=0
while read -r local_ref local_sha remote_ref remote_sha; do
    case "$local_ref" in
        refs/heads/{branch}) ;;
        refs/tags/v*)
            if [ "$local_sha" = "$zero" ]; then
                echo "pre-push: refusing to delete $remote_ref" >&2; status=1; continue
            fi
            commit=$(git rev-parse "$local_sha^{{commit}}" 2>/dev/null)
            if ! git merge-base --is-ancestor "$commit" refs/heads/{branch} 2>/dev/null; then
                echo "pre-push: refusing $local_ref: it does not point into {branch}" >&2; status=1
            fi ;;
        *)
            echo "pre-push: refusing to push $local_ref (only refs/heads/{branch} is public)" >&2; status=1 ;;
    esac
done
exit $status
"""


def git(*args, env=None, input=None, check=True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, env=env, input=input)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def forbidden_paths(paths) -> list[str]:
    bad = []
    for path in paths:
        if any(fnmatch.fnmatch(path, glob) for glob in ALLOWED):
            continue
        if any(fnmatch.fnmatch(path, glob) for glob in FORBIDDEN):
            bad.append(path)
    return bad


def public_commit(tree: str, message: str, parent: str | None = None) -> str:
    date = git("log", "-1", "--format=%cI", "HEAD")
    env = {**os.environ, "GIT_AUTHOR_NAME": AUTHOR_NAME, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
           "GIT_COMMITTER_NAME": AUTHOR_NAME, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
           "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    # -c commit.gpgsign=false is not needed: commit-tree signs only with -S.
    return git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message, env=env)


def update_public_ref(branch: str, tree: str, message: str) -> tuple[str, str]:
    """Advance ``refs/heads/<branch>`` to a commit of ``tree``; return ``(commit, old)``.

    The new commit's parent is the current tip (none on the first run), so the branch
    is append-only; an unchanged tree keeps the current tip.
    """
    ref = f"refs/heads/{branch}"
    old = git("rev-parse", "--verify", "--quiet", ref, check=False)
    if old and git("rev-parse", f"{old}^{{tree}}") == tree:
        return old, old
    commit = public_commit(tree, message, old or None)
    git("update-ref", "-m", "make_public_branch", ref, commit, *([old] if old else []))
    return commit, old


def install_hook(branch: str) -> Path:
    git_dir = Path(git("rev-parse", "--absolute-git-dir"))
    hooks = git_dir / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    if hook.exists() and HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
        hook.replace(hooks / "pre-push.local-backup")
    with open(hook, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(HOOK.format(marker=HOOK_MARKER, branch=branch))
    os.chmod(hook, 0o755)
    return hook


def activate_hook(hooks_dir: Path) -> None:
    # worktreeConfig is honoured with repositoryformatversion 0 (git >= 2.28 treats it
    # as a v0-compatible extension), so the format version is left alone.
    git("config", "extensions.worktreeConfig", "true")
    git("config", "--worktree", "core.hooksPath", hooks_dir.as_posix())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--branch", default="public")
    parser.add_argument("--message", help="commit message (default: 'Agent Nexus <version>')")
    parser.add_argument("--dry-run", action="store_true", help="check the tree and print the plan only")
    parser.add_argument("--activate-hook", action="store_true",
                        help="activate the pre-push hook even without a remote (writes extensions.worktreeConfig)")
    args = parser.parse_args(argv)

    if git("status", "--porcelain", "--untracked-files=no"):
        print("warning: uncommitted changes are not part of the public tree (it is built from HEAD)", file=sys.stderr)
    tree = git("rev-parse", "HEAD^{tree}")
    paths = git("ls-tree", "-r", "--name-only", "-z", tree).split("\0")
    paths = [path for path in paths if path]
    bad = forbidden_paths(paths)
    if bad:
        for path in bad:
            print("forbidden in the public tree:", path, file=sys.stderr)
        return 1
    workflows = [path for path in paths if path.startswith(".github/workflows/")]
    if not workflows:
        print("the public tree has no .github/workflows; refusing", file=sys.stderr)
        return 1
    sys.path.insert(0, str(ROOT / "scripts"))
    from version import read_version  # noqa: E402

    version = read_version(ROOT)
    message = args.message or f"Agent Nexus {version}"
    remotes = git("remote").split()
    if args.dry_run:
        print(f"would write {args.branch}: tree {tree}, {len(paths)} files, author {AUTHOR_NAME}")
        return 0

    commit, old = update_public_ref(args.branch, tree, message)
    hook = install_hook(args.branch)
    if "origin" in remotes:
        git("config", "remote.origin.push", f"refs/heads/{args.branch}:refs/heads/main")
        activate_hook(hook.parent)
        hook_state = "active (core.hooksPath for this worktree)"
    elif args.activate_hook:
        activate_hook(hook.parent)
        hook_state = "active (core.hooksPath for this worktree; no remote yet)"
    else:
        hook_state = "installed; activates when this script runs with an 'origin' remote (or --activate-hook)"
    print(f"{args.branch}: {commit} ({'unchanged' if old == commit else 'updated'}), "
          f"{len(paths)} files, {len(workflows)} workflows")
    print(f"pre-push hook: {hook} [{hook_state}]")
    print("remote.origin.push:", PUSH_REFSPEC if "origin" in remotes else "not set (no 'origin' remote)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
