# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path
import logging
import subprocess
import time
from config import (VERSIONING_EMAIL, VERSIONING_NAME, is_versioned_file,
                    vault_abs_root, vault_git_dir)
from src import timefmt
from difflib import unified_diff
import os

logger = logging.getLogger("docversioning")


class MarkdownGitVersioning:

    def __init__(self, markdown_folder: str, git_dir: str | None = None,
                 work_tree: str | None = None):
        """Initialize Git repository for the markdown folder.

        The container NEVER resolves the worktree's on-disk `.git` gitlink (its contents
        are host-facing -- see config's gitlink-direction note). Instead every git call
        pins the git-dir and work-tree explicitly. Callers pass a vault *root* whose
        basename is the slug, so the separated git-dir is derived via vault_git_dir; an
        override is accepted for non-standard layouts -- as it is for the work
        tree, which otherwise resolves through the vault registry.
        """
        self.folder = Path(markdown_folder)
        slug = self.folder.name
        self.git_dir = git_dir or vault_git_dir(slug)
        self.work_tree = work_tree or vault_abs_root(slug)
        self._init_repo()

    # stderr signatures of transient repo-lock contention (another git process
    # holds index.lock / a ref lock). One vault = one shared git-dir committed to by
    # BOTH the web server (user/agent/chat writes) and the worker (watcher commits),
    # so concurrent git on the same repo is normal and intermittently collides.
    _GIT_LOCK_SIGS = ("index.lock", "Unable to create", "cannot lock ref",
                      "another git process", "File exists")

    def _git_cmd(self, *args):
        """The argv every git call in this class runs. Shared so a failure raised
        outside _run_git still reports the exact command that failed."""
        return ["git", "-C", str(self.folder),
                f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}", *args]

    def _run_git(self, *args, check=True, env=None, retry=True):
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

        `retry=False` opts out for calls whose lock-shaped stderr is NOT transient:
        `update-ref`'s compare-and-swap failure reads "cannot lock ref ... is at X but
        expected Y", which retrying with the same stale old value can only repeat.
        _commit_staged handles that one by rebuilding on the new HEAD instead.
        """
        cmd = self._git_cmd(*args)
        result = self._spawn(cmd, env)
        for attempt in range(5):
            if result.returncode == 0 or not retry:
                break
            if not any(sig in (result.stderr or "") for sig in self._GIT_LOCK_SIGS):
                break
            time.sleep(0.1 * (attempt + 1))  # 0.1,0.2,..0.5s -> ~1.5s total
            result = self._spawn(cmd, env)
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, output=result.stdout, stderr=result.stderr)
        return result

    @staticmethod
    def _spawn(cmd: list[str], env: dict | None) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              check=False, env=env)

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

    # `update-ref`'s compare-and-swap rejection: "is at <sha> but expected <sha>".
    _CAS_FAIL_SIG = "but expected"
    # Cap on pathspecs per `git add` so a large batch cannot overflow ARG_MAX.
    _ADD_CHUNK = 256

    @staticmethod
    def _author_env(author_name: str, author_email: str) -> dict:
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email or "",
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email or "",
        })
        return env

    def _rel(self, file_path: str) -> str:
        """Vault-relative POSIX path for a path under the vault root, given absolute,
        relative to CWD (``vaults/<slug>/...``), or already vault-relative.

        self.folder is whatever the caller built the tracker on - relative
        vault_root (worker, content_ops) or absolute vault_abs_root (WikiDoc) - so
        try it first, then both forms of the work tree for the mixed cases."""
        p = Path(file_path)
        for base in (self.folder, Path(self.work_tree),
                     Path(os.path.relpath(self.work_tree))):
            try:
                return p.relative_to(base).as_posix()
            except ValueError:
                pass
        return p.as_posix()

    def _stage(self, rel_paths: list[str]) -> None:
        """Stage every path: additions, modifications AND removals -- `git add` on a
        path whose file is gone records its deletion. Pathspec-limited, so the cost
        is per-path and not per-repo; chunked to stay under ARG_MAX."""
        for i in range(0, len(rel_paths), self._ADD_CHUNK):
            self._run_git("add", "--", *rel_paths[i:i + self._ADD_CHUNK])

    def _head_and_tree(self) -> tuple[str, str]:
        """(HEAD sha, HEAD tree sha), or ('', '') on an unborn branch."""
        res = self._run_git("rev-parse", "HEAD", "HEAD^{tree}", check=False)
        if res.returncode != 0:
            return "", ""
        parts = res.stdout.split()
        return (parts[0], parts[1]) if len(parts) == 2 else ("", "")

    def _commit_staged(self, message: str, env: dict, what: str = "") -> str | None:
        """Commit whatever is staged. Returns the SHA, or None if nothing changed.

        Built from plumbing (write-tree -> commit-tree -> update-ref) rather than
        `git commit`, because porcelain commit first REFRESHES the index: an lstat of
        every tracked entry. Over the 9p vault mount that refresh IS the cost of a
        commit -- ~1.0s for a 500-file vault against ~0.03s to write the tree the
        index already describes -- and it scales with the vault, not the change. It
        buys nothing here: callers stage the exact paths they touched.

        Comparing the new tree to HEAD's is also a sounder "nothing to commit" test
        than parsing porcelain's message. It is content-based, so .gitattributes EOL
        normalization is accounted for (an on-disk CRLF file whose blob is LF reads as
        unchanged) and an empty commit is a quiet no-op rather than the exit-1 that
        once surfaced as a 500 on save.

        update-ref is a compare-and-swap against the HEAD we parented onto, so a
        commit racing the worker's watcher is rejected rather than silently dropping
        one side; the retry rebuilds the tree on the new HEAD.
        """
        for attempt in range(4):
            head, head_tree = self._head_and_tree()
            tree = self._run_git("write-tree").stdout.strip()
            if tree == head_tree:
                logger.debug("nothing to commit for %r (%s)", what or message, self.git_dir)
                return None
            parents = ("-p", head) if head else ()
            sha = self._run_git(
                "commit-tree", tree, *parents, "-m", message, env=env).stdout.strip()
            # An empty oldvalue asserts "HEAD must not exist yet" (initial commit).
            res = self._run_git("update-ref", "-m", message, "HEAD", sha, head,
                                check=False, retry=False)
            if res.returncode == 0:
                return sha
            stderr = res.stderr or ""
            if attempt < 3 and (self._CAS_FAIL_SIG in stderr
                                or any(sig in stderr for sig in self._GIT_LOCK_SIGS)):
                logger.info("HEAD moved under commit %r - rebuilding", what or message)
                time.sleep(0.05 * (attempt + 1))
                continue
            raise subprocess.CalledProcessError(
                res.returncode, self._git_cmd("update-ref", "HEAD"),
                output=res.stdout, stderr=res.stderr)

    def _commit(self, rel_path: str, message: str, author_name: str, author_email: str) -> str | None:
        """Stage a file and commit. Returns the commit SHA, or None if nothing changed."""
        self._stage([str(rel_path)])
        env = self._author_env(author_name, author_email)
        return self._commit_staged(message, env, what=str(rel_path))

    def commit_paths(
        self,
        file_paths: list[str],
        message: str,
        author_name: str = VERSIONING_NAME,
        author_email: str = VERSIONING_EMAIL,
    ) -> str | None:
        """Stage many paths and record ONE commit. Returns the SHA, or None when
        nothing changed.

        The batch entry point behind remove_files. Staging is pathspec-limited and
        cheap while a commit is not, so N paths cost N cheap `git add`s and a single
        commit -- the difference between a folder delete that scales with the folder
        and one that scales with the repo. Paths may be absolute or vault-relative and
        need not exist (a vanished path stages as a removal).
        """
        if not file_paths:
            return None
        self._init_repo()
        rels = [self._rel(p) for p in file_paths]
        self._stage(rels)
        env = self._author_env(author_name, author_email)
        what = rels[0] if len(rels) == 1 else f"{rels[0]} +{len(rels) - 1} more"
        commit_sha = self._commit_staged(message, env, what=what)
        self._maybe_update_commit_graph()
        return commit_sha

    def _is_text(self, file_path: str) -> bool:
        """Does this file belong in history at all -- i.e. is it UTF-8 text?

        The vault repo exists to diff prose; a committed binary bloats that history
        permanently and irreversibly (see config.is_versioned_file). The test is
        content-based rather than extension-based because the content writers also
        version text control files that are not DOCUMENT_FILE_TYPES -- a vault's
        .tzara/config.json and .gitattributes.
        """
        try:
            Path(file_path).read_text(encoding="utf-8")
            return True
        except (UnicodeDecodeError, OSError):
            return False

    def checkpoint_paths(self, file_paths: list[str], message: str) -> str | None:
        """Commit any pending edits to these paths, so a following mutation is
        cleanly revertable. Binaries are filtered out (see _is_text); an already-clean
        set is a no-op. The batch form of save_version's checkpoint role."""
        return self.commit_paths([p for p in file_paths if self._is_text(p)], message)

    def remove_file(
        self,
        file_path: str,
        author_name: str = VERSIONING_NAME,
        author_email: str = VERSIONING_EMAIL,
        message: str = None,
    ):
        return self.remove_files([file_path], author_name, author_email, message)

    def remove_files(
        self,
        file_paths: list[str],
        author_name: str = VERSIONING_NAME,
        author_email: str = VERSIONING_EMAIL,
        message: str = None,
    ):
        """Record the removal of many files in ONE commit.

        Content-blind like add_file/move_file: it stages paths without reading them,
        so a binary that leaked into history earlier can still be removed from it.
        """
        if not file_paths:
            return None
        if message is None:
            message = (f"Delete {Path(file_paths[0]).name}" if len(file_paths) == 1
                       else f"Delete {len(file_paths)} files")
        # Raw paths: commit_paths does the one _rel. Relativizing twice strips a
        # vault folder literally named vaults/<slug>/ a second time.
        return self.commit_paths(file_paths, message, author_name, author_email)

    def add_file(
        self,
        file_path: str,
        author_name: str = VERSIONING_NAME,
        author_email: str = VERSIONING_EMAIL,
        message: str = None,
    ):
        """Record a newly created file (copy, import) in git history.

        Only first-class DOCUMENTS are committed (config.is_versioned_file): an
        attachment lives in the vault and is served from it, but must never enter
        the history, where a binary would bloat the repo permanently. Non-documents
        return None -- the caller's file is already written to disk, so this refuses
        the commit, not the operation.

        Content-blind otherwise, like remove_file/move_file: it stages the path
        without reading it (unlike save_version, which decodes the file to diff it
        against HEAD and would raise on binary content).
        """
        rel_path = Path(file_path).relative_to(self.folder)

        if not is_versioned_file(str(rel_path)):
            logger.info("add_file: not a versioned document type, skipping %s", rel_path)
            return None

        self._init_repo()

        if message is None:
            message = f"Add {rel_path.name}"

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

        env = self._author_env(author_name, author_email)
        commit_sha = self._commit_staged(message, env, what=f"{old_rel} -> {new_rel}")
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
            or the file is not a text document.
        """
        self._init_repo()

        rel_path = Path(file_path).relative_to(self.folder)
        file_full_path = self.folder / rel_path

        # Reading the file is both the change check and the binary gate: a file that
        # is not UTF-8 text must not enter history, and decoding one used to raise
        # straight out of here (callers swallowed it, and a delete then removed the
        # file from disk without ever recording the removal).
        if not self._is_text(str(file_full_path)):
            logger.info("save_version: %s is not UTF-8 text - not versioning it", rel_path)
            return None
        current_content = file_full_path.read_text(encoding="utf-8")

        # Unchanged against HEAD -> nothing to do. A missing HEAD or a file not yet in
        # it (new repo, new file) falls through to the commit.
        head_result = self._run_git("show", f"HEAD:{rel_path}", check=False)
        if head_result.returncode == 0 and current_content == head_result.stdout:
            return None

        if message is None:
            message = f"Update {rel_path.name}"

        commit_sha = self._commit(str(rel_path), message, author_name, author_email)
        self._maybe_update_commit_graph()
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
