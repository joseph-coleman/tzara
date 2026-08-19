# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path
import logging
import subprocess
import time
from config import VERSIONING_EMAIL, VERSIONING_NAME, vault_abs_root, vault_git_dir
from src import timefmt
from difflib import unified_diff
import os

logger = logging.getLogger("docversioning")


class MarkdownGitVersioning:

    def __init__(self, markdown_folder: str, git_dir: str | None = None):
        """Initialize Git repository for the markdown folder.

        The container NEVER resolves the worktree's on-disk `.git` gitlink (its contents
        are host-facing -- see config's gitlink-direction note). Instead every git call
        pins the git-dir and work-tree explicitly. Callers pass a vault *root* whose
        basename is the slug, so the separated git-dir is derived via vault_git_dir; an
        override is accepted for non-standard layouts.
        """
        self.folder = Path(markdown_folder)
        slug = self.folder.name
        self.git_dir = git_dir or vault_git_dir(slug)
        self.work_tree = vault_abs_root(slug)
        self._init_repo()

    # stderr signatures of transient repo-lock contention (another git process
    # holds index.lock / a ref lock). One vault = one shared git-dir committed to by
    # BOTH the web server (user/agent/chat writes) and the worker (watcher commits),
    # so concurrent git on the same repo is normal and intermittently collides.
    _GIT_LOCK_SIGS = ("index.lock", "Unable to create", "cannot lock ref",
                      "another git process", "File exists")

    def _run_git(self, *args, check=True, env=None):
        """Execute a git command against this vault's separated repo.

        --git-dir/--work-tree are pinned explicitly (absolute, container-side) so git
        never reads the host-facing `.git` gitlink; -C keeps cwd at the worktree so
        relative pathspecs resolve as before.

        Retries transient index/ref-LOCK contention (a concurrent git process on the
        same shared repo -> exit 128 'Unable to create index.lock'). The op itself is
        valid; the lock is transiently held, so a bounded backoff is correct -- this
        was the residual `git add` exit-128 flake that per-file debounce couldn't
        cover (cross-file contention between the server's commit and the worker's
        watcher commit).
        """
        cmd = ["git", "-C", str(self.folder),
               f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}", *args]
        result = None
        for attempt in range(6):
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                check=False, env=env,
            )
            if result.returncode == 0:
                return result
            stderr = result.stderr or ""
            if attempt < 5 and any(sig in stderr for sig in self._GIT_LOCK_SIGS):
                time.sleep(0.1 * (attempt + 1))  # 0.1,0.2,..0.5s -> ~1.5s total
                continue
            break
        if check and result is not None and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, output=result.stdout, stderr=result.stderr)
        return result

    def _init_repo(self):
        """Ensure a git repository exists at self.folder. Idempotent AND cheap.

        This runs on EVERY MarkdownGitVersioning construction (i.e. every commit/
        move/delete). Re-running the full sequence every op was both wasteful
        (~8 subprocess calls) and the source of intermittent `git` exit 128/255
        under concurrency: the unconditional `commit-graph write --reachable`
        (removed) took a lock that raced the watcher's own git ops. Commit-graph
        maintenance is now owned solely by the throttled _maybe_update_commit_graph
        (every COMMIT_GRAPH_INTERVAL commits), so it is NOT run here.

        Short-circuit only when the repo is BOTH initialized (HEAD present) AND
        already carries our config - detected by a cheap config-FILE read (no
        subprocess) for the `ignorecase` sentinel we set. That sentinel is written
        in the same sequence as the `core.worktree` unset, so its presence implies
        the poisoning fix was applied. A repo with HEAD but WITHOUT our config
        (partial/external init) falls through and gets configured, so host-git
        poisoning can never silently persist.
        """
        head = os.path.join(self.git_dir, "HEAD")
        cfg = os.path.join(self.git_dir, "config")
        if os.path.exists(head):
            try:
                with open(cfg, "r", encoding="utf-8") as fh:
                    # git preserves the key's case as written (core.ignoreCase ->
                    # "ignoreCase = true" in the file), so match case-insensitively.
                    if "ignorecase = true" in fh.read().lower():
                        return  # initialized + our config applied -> nothing to do
            except OSError:
                pass  # config unreadable -> fall through and (re)apply
        if not os.path.exists(head):
            self._run_git("init", "-b", "main")
        # `git init` with a separated --work-tree persists core.worktree into the
        # git-dir config (an absolute, CONTAINER-side path like /app/app/vaults/x).
        # That value is redundant for us -- every _run_git pins --work-tree on the
        # command line, which overrides config -- but it POISONS the shared git-dir
        # for host-side (Windows) git, which follows the vault's `.git` gitlink into
        # this config and dies with `fatal: Invalid path '/app'`. Strip it so the
        # git-dir stays portable; when git reaches it via the gitlink it derives the
        # worktree from the gitlink's own location. check=False: absent on re-init.
        self._run_git("config", "--unset", "core.worktree", check=False)
        self._run_git("config", "core.fileMode", "false")
        self._run_git("config", "core.ignoreCase", "true")
        self._run_git("config", "core.commitGraph", "true")
        self._run_git("config", "gc.writeCommitGraph", "true")
        self._run_git("config", "commitGraph.changedPaths", "true")
        # Disable BACKGROUND auto-gc. It races the high-frequency agent / canvas /
        # metadata commit writers: a DETACHED gc computing reachability while refs
        # move can sweep a still-reachable object into the prune/cruft path, leaving
        # a broken parent link (the missing-commit corruption diagnosed 2026-07-09,
        # whose repack was stamped the same minute as the orphaning commit). Run gc
        # manually only when the repo is quiescent instead of letting it fire under
        # concurrent writers.
        self._run_git("config", "gc.auto", "0")
        self._run_git("config", "gc.autoDetach", "false")
        # Harden object durability across the WSL2/NTFS bridge and frequent
        # container kills: fsync loose objects, packs, and refs so an interrupted
        # write can't leave a truncated/missing object. (git >= 2.36 list syntax.)
        self._run_git("config", "core.fsync", "loose-object,pack,ref")


    _commit_count = 0
    COMMIT_GRAPH_INTERVAL = 20

    def _maybe_update_commit_graph(self):
        """Kick off a background task to regenerate the commit-graph every N commits."""
        MarkdownGitVersioning._commit_count += 1
        if MarkdownGitVersioning._commit_count % self.COMMIT_GRAPH_INTERVAL != 0:
            return
        try:
            from src.task_definitions import update_commit_graph_task
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(update_commit_graph_task.kicker().kiq())
            else:
                asyncio.run(update_commit_graph_task.kicker().kiq())
        except Exception:
            # Fall back to inline if task broker unavailable
            self._update_commit_graph()

    def _update_commit_graph(self):
        """Regenerate the commit-graph file to speed up history traversal."""
        try:
            self._run_git("commit-graph", "write", "--reachable", "--changed-paths")
        except subprocess.CalledProcessError:
            pass  # no HEAD yet

    def _commit(self, rel_path: str, message: str, author_name: str, author_email: str) -> str:
        """Stage a file and commit. Returns the commit SHA."""
        self._run_git("add", "--", str(rel_path))

        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email or "",
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email or "",
        })
        self._run_git("commit", "-m", message, env=env)
        return self._run_git("rev-parse", "HEAD").stdout.strip()

    def remove_file(
        self,
        file_path: str,
        author_name: str = VERSIONING_NAME,
        author_email: str = VERSIONING_EMAIL,
        message: str = None,
    ):
        self._init_repo()

        rel_path = Path(file_path).relative_to(self.folder)

        if message is None:
            message = f"Delete {rel_path.name}"

        commit_sha = self._commit(str(rel_path), message, author_name, author_email)
        self._maybe_update_commit_graph()
        return commit_sha

    def move_file(
        self,
        old_path: str,
        new_path: str,
        author_name: str = VERSIONING_NAME,
        author_email: str = VERSIONING_EMAIL,
        message: str = None,
    ):
        """Record a file move/rename in git history."""
        self._init_repo()

        old_rel = Path(old_path).relative_to(self.folder)
        new_rel = Path(new_path).relative_to(self.folder)

        # Stage the removal of the old path (may already be gone from disk)
        self._run_git("rm", "--cached", "--ignore-unmatch", str(old_rel))
        # Stage the new file
        self._run_git("add", "--", str(new_rel))

        if message is None:
            message = f"Move {old_rel.name} to {new_rel}"

        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email or "",
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email or "",
        })
        self._run_git("commit", "-m", message, env=env)
        commit_sha = self._run_git("rev-parse", "HEAD").stdout.strip()
        self._maybe_update_commit_graph()
        return commit_sha

    def has_file_changed(self, file_path: str):
        """Has file changed in working tree since HEAD?"""
        rel_path = Path(file_path).relative_to(self.folder)
        result = self._run_git("status", "--porcelain", "--", str(rel_path), check=False)
        return len(result.stdout.strip()) > 0

    def save_version(
        self,
        file_path: str,
        author_name: str = VERSIONING_NAME,
        author_email: str = VERSIONING_EMAIL,
        message: str = None,
    ) -> str | None:
        """
        Commit changes to a specific file if it has changed.

        Args:
            file_path: Path to the file relative to the markdown folder
            author_name: Name of the author making the change
            author_email: Email of the author
            message: Commit message (auto-generated if None)

        Returns:
            The commit SHA if changes were committed, None if no changes detected
        """
        self._init_repo()

        rel_path = Path(file_path).relative_to(self.folder)

        # Check if file has changed compared to HEAD
        try:
            head_result = self._run_git("show", f"HEAD:{rel_path}", check=True)
            # Compare HEAD content with current file on disk
            file_full_path = self.folder / rel_path
            current_content = file_full_path.read_text(encoding="utf-8")
            if current_content == head_result.stdout:
                return None
        except subprocess.CalledProcessError:
            # No HEAD or file not in HEAD (new repo or new file) - proceed to commit
            pass

        if message is None:
            message = f"Update {rel_path.name}"

        commit_sha = self._commit(str(rel_path), message, author_name, author_email)
        self._update_commit_graph()
        return commit_sha

    def file_in_repo(self, file_path: str):
        """Checks if a file exists in the HEAD of the current branch."""
        rel_path = Path(file_path).relative_to(self.folder)
        rel_path = str(rel_path).strip("/")
        result = self._run_git("cat-file", "-e", f"HEAD:{rel_path}", check=False)
        return result.returncode == 0

    def get_file_history(self, file_path: str, max_count: int = 10, cursor: str = None):
        """
        Get the commit history for a specific file with cursor-based pagination.

        Args:
            file_path: Path to the file relative to the markdown folder
            max_count: Maximum number of commits to return per page
            cursor: Commit SHA to start from (for pagination). If None, starts from HEAD.

        Returns:
            Dictionary with 'commits' list and 'next_cursor' (or None if no more)
        """
        FIELD_SEP = "\x00"
        RECORD_SEP = "\x01"
        rel_path = Path(file_path).relative_to(self.folder)

        fmt = "%H%x00%an%x00%ae%x00%B%x00%ct%x01"

        args = ["log", f"--format={fmt}", f"-{max_count + 1}"]
        if cursor:
            args.append(cursor)
        args.extend(["--", str(rel_path)])

        result = self._run_git(*args, check=False)
        # Salvage partial output on non-zero exit. `git log -- <path>` triggers
        # history SIMPLIFICATION, which for sparse-history files (fewer commits than
        # the -N limit) walks the FULL ancestry and exits non-zero if it hits a
        # broken/missing object - the known intermittent 128/255, or hard corruption
        # like a missing parent commit. git still prints every path-touching commit
        # NEWER than the break before failing (anything older is already
        # unreachable), so parse what reached stdout instead of discarding a valid,
        # if truncated, history. Only genuinely empty output means "no history".
        if not result.stdout.strip():
            return {"commits": [], "next_cursor": None}
        if result.returncode != 0:
            logger.warning(
                "git log exited %s for %s - returning commits parsed from partial "
                "output; run `git fsck` on the vault repo (history corruption).",
                result.returncode, rel_path)

        entries = [e.strip() for e in result.stdout.split(RECORD_SEP) if e.strip()]
        commits = []

        for entry in entries:
            parts = entry.split(FIELD_SEP, 4)
            if len(parts) < 5:
                continue

            sha, author_name, author_email, message_text, commit_time = parts

            if len(commits) >= max_count:
                return {"commits": commits, "next_cursor": sha}

            commits.append({
                "count": len(commits),
                "file_exists": True,
                "sha": sha,
                "short_sha": sha[:10],
                "message": message_text.strip(),
                "author": author_name,
                "email": author_email,
                "date": timefmt.to_local(int(commit_time)),
                "date_str": timefmt.stamp(int(commit_time)),
            })

        return {"commits": commits, "next_cursor": None}

    def get_file_at_commit(self, file_path: str, commit_sha: str) -> str:
        """
        Get the content of a file at a specific commit.

        Args:
            file_path: Path to the file relative to the markdown folder
            commit_sha: The commit SHA to retrieve

        Returns:
            The file content as a string
        """
        rel_path = Path(file_path).relative_to(self.folder)
        try:
            result = self._run_git("show", f"{commit_sha}:{rel_path}", check=True)
            return result.stdout
        except subprocess.CalledProcessError:
            raise FileNotFoundError(f"File {rel_path} not found in commit {commit_sha}")

    def revision_info(self, file_path: str, commit_sha: str) -> dict:
        """
        Describe a revision of a file.

        Returns {"short_sha", "date_str", "message", "commits_since"} where
        commits_since counts the commits that touched this file between the
        revision and HEAD (0 when the revision IS the file's current state).

        Best-effort: any git failure yields {} rather than raising, since callers
        use this for labels and warnings that must not break the surrounding
        operation.
        """
        FIELD_SEP = "\x00"
        rel_path = Path(file_path).relative_to(self.folder)

        # %x00 is git's escape for the separator - a literal NUL in the argv
        # string would truncate the argument at the C string terminator.
        show = self._run_git(
            "show", "-s", "--format=%h%x00%ct%x00%s", commit_sha, check=False)
        if show.returncode != 0:
            return {}
        parts = show.stdout.strip().split(FIELD_SEP, 2)
        if len(parts) < 3:
            return {}
        short_sha, commit_time, message_text = parts

        count = self._run_git(
            "rev-list", "--count", f"{commit_sha}..HEAD", "--", str(rel_path),
            check=False)
        try:
            commits_since = int(count.stdout.strip()) if count.returncode == 0 else 0
        except ValueError:
            commits_since = 0

        try:
            date_str = timefmt.stamp(int(commit_time))
        except ValueError:
            date_str = ""

        return {
            "short_sha": short_sha,
            "date_str": date_str,
            "message": message_text.strip(),
            "commits_since": commits_since,
        }

    def get_diff(self, file_path: str, old_commit: str, new_commit: str = None) -> str:
        """
        Get the diff between two versions of a file.

        Args:
            file_path: Path to the file relative to the markdown folder
            old_commit: The older commit SHA
            new_commit: The newer commit SHA (defaults to HEAD)

        Returns:
            Unified diff as a string
        """
        rel_path = Path(file_path).relative_to(self.folder)
        new_ref = new_commit if new_commit else "HEAD"
        result = self._run_git("diff", f"{old_commit}..{new_ref}", "--", str(rel_path), check=False)
        if result.stdout.strip():
            return result.stdout
        return "No differences found"

    def _get_file_content_at_sha(self, commit_sha: str, rel_path: str) -> str | None:
        """Get file content at a specific commit, or None if missing."""
        result = self._run_git("show", f"{commit_sha}:{rel_path}", check=False)
        if result.returncode != 0:
            return None
        return result.stdout

    def get_diff_history(self, file_path: str) -> str:
        """Get diffs across the full commit history of a file."""
        FIELD_SEP = "\x00"
        RECORD_SEP = "\x01"
        rel_path = str(Path(file_path).relative_to(self.folder))

        fmt = "%H%x00%P%x00%B%x01"
        result = self._run_git("log", f"--format={fmt}", "--reverse", "--", rel_path, check=False)

        if result.returncode != 0 or not result.stdout.strip():
            return ""

        entries = [e.strip() for e in result.stdout.split(RECORD_SEP) if e.strip()]
        response = []

        for entry in entries:
            parts = entry.split(FIELD_SEP, 2)
            if len(parts) < 3:
                continue
            sha, parents_str, message_text = parts
            parents = parents_str.strip().split()

            if not parents:
                continue  # initial commit

            parent_sha = parents[0]
            old = self._get_file_content_at_sha(parent_sha, rel_path)
            new = self._get_file_content_at_sha(sha, rel_path)

            if old == new:
                continue

            old_lines = (old or "").splitlines(keepends=True)
            new_lines = (new or "").splitlines(keepends=True)

            diff = unified_diff(
                old_lines,
                new_lines,
                fromfile=f"{parent_sha}:{rel_path}",
                tofile=f"{sha}:{rel_path}",
            )

            response.append(f"\nCommit {sha}")
            response.append(message_text.strip())
            response.append("".join(diff))

        return "\n".join(response)
