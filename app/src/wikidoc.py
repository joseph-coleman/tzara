# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import os
import re
from copy import deepcopy
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

logger = logging.getLogger("wikidoc")

from config import (  # DIRECTORY_AS_MD_FILE_LINK,; HIDE_DOT_DIRECTORY,
    ATTACHMENT_FILE_TYPES,
    DEFAULT_ENCODING,
    DEFAULT_VAULT,
    DEFAULT_WIKI_PAGE,
    DOCUMENT_FILE_TYPES,
    RESERVED_FILE_TYPES,
    RESERVED_PATHS,
    TEMPLATE,
    SPACE_CONVERSION_ORDER,
    is_template,
    vault_abs_root,
    vault_root,
)
from src.chunker import normalize_separators, separator_rank, wikilink_key
from src.vault_registry import vault_default_page


class WikiDoc:
    """A wiki document as defined by a URL

    Static Methods:
    parse_url_path
    markdown_page_name
    wikilink_page_check
    markdown_file_exists

    ----
    maybe splt some stuff into wikidoc helper functions?

    WikiDoc init, load
        - detect if file exists
        - load content? or do that lazily?
        -

    I should be able to create a doc that doesn't exist
    and then SET data and then save it.

    Setting data should allow me to edit a doc.

    """

    def __init__(self, url_path=None, file_path=None, vault=DEFAULT_VAULT):
        # The vault this document lives in. It is an orthogonal axis to the URL path:
        # the same vault-relative path can exist in many vaults. Construct WikiDoc with
        # a vault-STRIPPED action path (e.g. "/wiki/notes/foo.md") plus vault=, so
        # parse_url_path and all internal WikiDoc("/temp/...") callers stay unchanged.
        self._vault = vault or DEFAULT_VAULT
        self.__loaded = False
        if url_path:
            self.__url_path = url_path
            self.load_document(url_path)

        if file_path:
            # TODO load document by file path to markdown file
            ...

    def load_document(self, url_path):
        """Loads a document given a URL path"""

        assert (
            not self.__loaded
        ), f"Cannot load {url_path} again since path was already specified on instantiation."

        self.__url_path = url_path
        self.url_pieces = WikiDoc.parse_url_path(
            url_path, default_page=vault_default_page(self._vault)
        )

        self._path = self.url_pieces["path"]
        self._path_list = self.url_pieces["path_list"]
        self._file_name = self.url_pieces["file_name"]
        self._file_ext = self.url_pieces["file_ext"]
        self._file_name_no_ext = self.url_pieces["file_name_no_ext"]
        self._is_default_page_name = self.url_pieces["is_default_page_name"]

        _ = self.exists()

        self.__loaded = True

    @staticmethod
    def _test_existence(path=None, url_pieces=None, vault=DEFAULT_VAULT):
        """What calls this?"""
        _url_pieces = None
        if path:
            _url_pieces = WikiDoc.parse_url_path(path)
        if url_pieces:
            _url_pieces = deepcopy(url_pieces)
        if not _url_pieces:
            return False, None, None

        file_name = _url_pieces["file_name"]
        # file_path = _url_pieces["file_path"]
        path_list = _url_pieces["path_list"]
        file_ext = _url_pieces["file_ext"]
        file_path = ""

        if file_name == "favicon.ico":
            file_path = os.path.join(os.getcwd(), file_name)
            if Path(file_path).exists():
                # print("faveico !! !! !!")
                return True, file_path, file_name

        if path_list[0] == "template":
            path_list.pop(0)
            # The second segment NAMES the theme (base.html emits the active one).
            # Validated against the real theme folders, so an unknown or absent
            # segment -- "/template/theme.css", or a path from an older cached page
            # -- degrades to the site-wide TEMPLATE, exactly as before this segment
            # carried meaning. Themes are per-VAULT, so the name has to travel in the
            # URL: a stylesheet is fetched on its own, with no vault context, and two
            # vaults sharing one asset URL would share one browser cache entry.
            seg = path_list.pop(0) if path_list else None
            theme = seg if is_template(seg) else TEMPLATE

            if file_ext in RESERVED_FILE_TYPES:
                # Try the requested theme first
                template_path = os.path.join("template", theme)
                file_path = os.path.join(
                    os.getcwd(), template_path, *path_list, file_name
                )
                try:
                    WikiDoc.validate_path(
                        file_path, os.path.join(os.getcwd(), template_path)
                    )
                    resolved = WikiDoc._resolve_case_insensitive(file_path)
                    if resolved:
                        return True, resolved, file_name
                except ValueError:
                    pass

                # Fall back to default theme
                if theme != "default":
                    default_path = os.path.join("template", "default")
                    file_path = os.path.join(
                        os.getcwd(), default_path, *path_list, file_name
                    )
                    try:
                        WikiDoc.validate_path(
                            file_path, os.path.join(os.getcwd(), default_path)
                        )
                        resolved = WikiDoc._resolve_case_insensitive(file_path)
                        if resolved:
                            return True, resolved, file_name
                    except ValueError:
                        pass

                return False, file_path, file_name

        ## /wiki/* or everything else.
        # All SPACE_CONVERSION_ORDER chars are interchangeable: resolution folds
        # them (and case) so [[Game Ideas]] finds Game_Ideas.md, and even a
        # mixed-separator "My_File Copy.md" resolves. See _resolve_name_in_dir.
        stem = _url_pieces["file_name_no_ext"]
        wiki_base = vault_abs_root(vault)
        wiki_dir = os.path.join(wiki_base, *path_list)

        if file_ext == "":
            allowed_exts = {".md"}  # bare (extension-less) URL -> markdown page
        elif file_ext in DOCUMENT_FILE_TYPES:
            # First-class vault DOCUMENT (md, canvas, ...) -- viewed/edited as a page.
            # Sourced from the single DOCUMENT_FILE_TYPES list so a new document type
            # can't be resolvable in one place but forgotten here (the gap that made
            # .canvas render an EMPTY canvas). Resolve by exact extension.
            allowed_exts = {"." + file_ext}
        elif file_ext in ATTACHMENT_FILE_TYPES:
            # A vault attachment (Obsidian model): user content dropped next to a
            # page. Resolve by exact extension so it can be served as a static asset
            # and referenced from cell code. The allowlist (ATTACHMENT_FILE_TYPES) is
            # the safety boundary -- unknown/active types are never served off disk.
            allowed_exts = {"." + file_ext}
        else:
            return False, "", file_name

        # Default (non-existent) path, used for the not-found return.
        file_path = os.path.join(wiki_dir, file_name) + (".md" if file_ext == "" else "")

        try:
            WikiDoc.validate_path(wiki_dir, wiki_base)
        except ValueError:
            return False, file_path, file_name

        resolved = WikiDoc._resolve_name_in_dir(wiki_dir, stem, allowed_exts)
        if resolved:
            resolved_stem, resolved_ext = os.path.splitext(os.path.basename(resolved))
            # Keep the resolved name's ext-presence matching file_ext, so it stays
            # consistent with _file_ext for _encode() (markdown) or used verbatim.
            resolved_name = resolved_stem + (resolved_ext if file_ext != "" else "")
            return True, resolved, resolved_name

        # Not found. For a brand-new markdown file, normalize the basename to the
        # preferred separator so [[New Idea]] / [[My File Copy]] create New_Idea.md
        # / My_File_Copy.md rather than verbatim or mixed-separator names. Only the
        # basename is touched; path_list (folders) is left as-is so save()'s
        # makedirs and the file write stay in sync.
        if file_ext in ("", "md") and stem and SPACE_CONVERSION_ORDER:
            new_stem = normalize_separators(stem, SPACE_CONVERSION_ORDER[0])
            new_name = new_stem + (".md" if file_ext == "md" else "")
            new_path = os.path.join(wiki_dir, new_stem) + ".md"
            return False, new_path, new_name

        return False, file_path, file_name

    def exists(self):
        if not self.url_pieces:
            return False

        # if hasattr(self, "_exists"):
        #     return self._exists

        self._exists, self._file_path, self._file_name = WikiDoc._test_existence(
            url_pieces=self.url_pieces, vault=self._vault
        )

        if self._exists:
            return self._file_path

        return False

    # @staticmethod
    # def file_exists(path):
    #     return "filepath"  # if exists

    def extension(self):
        if hasattr(self, "_file_ext"):
            return self._file_ext
        return None

    # def file_name(self):
    #     if hasattr(self, "_file_name"):
    #         return self._file_name
    #     return None

    def vault(self):
        return self._vault

    def _encode(self, *, with_prefix: bool, with_extension: bool):
        """Single source of truth for assembling a markdown doc's FILESYSTEM path.

        with_prefix=True  -> prepends the vault's working-tree root ("vaults/{vault}")
        with_extension=True -> ensures the result ends with ".md"
        Returns None if the doc isn't loaded or isn't a markdown file.

        NOTE: this builds *filesystem* paths. For a user-facing URL use
        ``display_url_path`` (which uses the "/wiki/{vault}/..." namespace), since the
        on-disk directory ("vaults/{vault}") and the URL prefix ("/wiki/{vault}") are
        no longer the same string under multi-vault.
        """
        if not self.__loaded or self._file_ext not in ("", "md"):
            return None
        prefix = [vault_root(self._vault)] if with_prefix else []
        parts = prefix + list(self._path_list) + [self._file_name]
        out = os.path.join(*parts) if parts else ""
        if with_extension and self._file_ext == "":
            out += ".md"
        elif not with_extension and self._file_ext == "md":
            out = out[:-3]
        return out

    def normalized_url_path(self):
        """Filesystem path under the vault root, e.g. 'vaults/main/notes/foo.md'.

        Despite the legacy name this is the canonical internal *filesystem* identifier
        (version-tracker paths, generate-task file_path args). The vault-relative
        doc_id is relative_file_path(); the vault itself is a separate axis (vault()).
        """
        return self._encode(with_prefix=True, with_extension=True)

    def relative_file_path(self):
        return self._encode(with_prefix=False, with_extension=True)

    def relative_display_path(self):
        """Vault-relative, extension-less path, e.g. 'notes/foo' (no vault, no '.md').

        The user-facing "path relative to the vault" form: display_url_path() minus
        its leading 'wiki/{vault}/'. Used to prefill the move/rename prompt so the
        value the user edits is exactly what /api/move expects as a vault-relative
        destination (paired with an explicit ``vault`` field).
        """
        return self._encode(with_prefix=False, with_extension=False)

    def display_url_path(self):
        """User-facing URL path with no `.md` extension, e.g. 'wiki/{vault}/notes/foo'.

        Use this for redirects, links, and any other string the user will see in the
        address bar or as a clickable href. Action-first: the vault follows the verb,
        so a vault may be named anything without colliding with a route. This form is
        also what edit/save forms round-trip as ``document_name`` (re-parsed by
        ``from_url_with_vault``).
        """
        rel = self._encode(with_prefix=False, with_extension=False)
        if rel is None:
            return None
        return f"wiki/{self._vault}/{rel}"

    @staticmethod
    def from_url_with_vault(url, default_vault=DEFAULT_VAULT):
        """Build a WikiDoc from a *vaulted* URL/identifier like
        '/wiki/{vault}/notes/foo.md', 'wiki/{vault}/notes/foo', or
        '/{vault}/notes/foo.canvas'.

        Strips an optional leading action verb (wiki/edit/save/...), takes the next
        segment as the vault, and parses the remainder as the document path. Used for
        form round-trips (save/delete document_name, canvas doc_name) and any handler
        that receives a full user-facing path rather than separate route params. The
        vault is NOT validated here -- callers should check vault_registry.vault_exists.
        """
        cleaned = unquote(url or "").replace("\\", "/")
        while ".." in cleaned:
            cleaned = cleaned.replace("..", "")
        parts = [p for p in cleaned.split("/") if p]
        if parts and parts[0] in RESERVED_PATHS:
            parts.pop(0)
        vault = parts.pop(0) if parts else default_vault
        rest = "/".join(parts)
        return WikiDoc("/wiki/" + rest, vault=vault)

    def is_default(self):
        if not hasattr(self, "_is_default_page_name"):
            return True

        return self._is_default_page_name

    def path(self):
        if hasattr(self, "_path"):
            return self._path
        return None

    def path_list(self):
        if hasattr(self, "_path_list"):
            return self._path_list
        return []

    # @property
    def file_path(self):
        if hasattr(self, "_file_path"):
            return self._file_path
        return False

    # @property
    def file_name(self):
        """Returns file name even if file does not exist"""
        if hasattr(self, "_file_name"):
            return self._file_name
        return None

    def file_name_no_ext(self):
        """Returns file name without md extension"""
        if hasattr(self, "_file_name_no_ext"):
            return self._file_name_no_ext
        return None

    def refresh_content(self):
        if hasattr(self, "_content"):
            del self._content

        return self.get_content(data_type=self._data_type)

    def get_content(self, data_type="text"):
        if hasattr(self, "_content"):
            # print("CACHED content")
            # print("@#@#@# --> ")
            # print("has attribute self content")
            # print(self._content)
            return self._content
        self._content = None
        self._data_type = None
        if not hasattr(self, "_exists"):
            # print("##$$##$$# not hasatrib exits")
            if not self.exists():
                return self._content  # or raise or assert?
                assert (
                    self.exists()
                ), "Trying to read the contents of a file that does not exist."

        if hasattr(self, "_exists") and self._exists:
            if data_type == "text":
                # print("Reading file as text ", self._file_path)
                # Canonical read: newline='' preserves CRLF; shared with the
                # (vault, rel) read_text primitive via _read_raw.
                self._content = WikiDoc._read_raw(self._file_path)
                self._data_type = "text"
            if data_type == "binary":
                # print("Reading file as binary ", self._file_path)
                with open(self._file_path, "rb") as file:
                    self._content = file.read()
                    self._data_type = "binary"
        else:
            # print("->->->->->->->  file doesn't exist?")
            # print(self.url_pieces)
            # print("what should the default be?")
            self._content = ""
            self._data_type = "text"

        return self._content

    def set_content(self, content, data_type="text"):
        self._content = content
        self._data_type = data_type

    def save(self, content=None, data_type=None, overwrite_default=False):

        path_list = self._path_list

        WikiDoc.validate_path(self._file_path, vault_abs_root(self._vault))

        os.makedirs(os.path.join(vault_root(self._vault), *path_list), exist_ok=True)

        if not content:
            updated_content = self._content
        else:
            updated_content = content

        if not data_type:
            data_type = self._data_type

        if self.is_default() and not overwrite_default:
            assert False, f"Cannot overwrite default page {self._file_path}"

        if data_type == "text":
            # EOL-preserving write. The editor/canvas post content with browser
            # line endings (typically LF); without this, saving a page that was
            # CRLF on disk (Windows/Obsidian) flattens the whole file to LF and
            # churns every line in git. Detect the existing file's EOL, normalize
            # the incoming content to LF, then re-apply - the same guarantee
            # commit()/write_text give the (vault, rel) callers. New files default
            # to LF. Shares the single low-level open via _read_raw/_write_raw.
            eol = "\n"
            if os.path.isfile(self._file_path):
                existing = WikiDoc._read_raw(self._file_path)
                eol = "\r\n" if "\r\n" in existing else "\n"
            lf = updated_content.replace("\r\n", "\n")
            out = lf.replace("\n", "\r\n") if eol == "\r\n" else lf
            WikiDoc._write_raw(self._file_path, out)
        elif data_type == "binary":
            with open(
                self._file_path,
                "wb",
            ) as file:
                file.write(updated_content)
        else:
            assert (
                False
            ), f"Unknown save data type is not binary or text, it is {data_type}"

    def delete(self, delete_default=False):

        # # This is bad code.
        # self.exists()
        # if self._exists:
        #     file_path = self._file_path
        #     if len(file_path) > 0:  # is this ever going to be zero?
        #         if Path(file_path).exists():  # double check?
        #             os.remove(file_path)
        #             return True

        # this implementation forces us to look only in /wiki/ for files to delete.

        path_list = self._path_list
        file_name = self._file_name
        file_ext = self._file_ext

        file_path = ""
        if file_ext == "":
            file_name = file_name + ".md"
            file_ext = "md"

        file_path = os.path.join(vault_root(self._vault), *path_list, file_name)

        # prevent us from deleting the default page by mistake.
        if self.is_default() and not delete_default:
            return False

        try:
            WikiDoc.validate_path(file_path, vault_root(self._vault))
        except ValueError:
            return False

        if len(file_path) > 0:
            if Path(file_path).exists():
                os.remove(file_path)
                return True
        return False

    # @staticmethod
    # def create_url_path(path, filename):
    #     # TODO
    #     return path + "/" + filename

    # -----------------------------------------------------------------------
    # Canonical (vault, rel) document I/O primitives.
    #
    # These are THE home for document reads/writes/commits - every caller
    # routes through them so the safeguards (EOL preservation, DEFAULT_ENCODING,
    # xmlcharrefreplace, traversal validation, checkpoint-before-mutate, git
    # attribution, watcher debounce) can never be shed by a new code path.
    # They are @staticmethod because callers hold bare (vault, rel) tuples, not
    # constructed WikiDoc instances (same shape as markdown_file_exists). See
    # FILE_OPS_DRIFT_REMEDIATION_PLAN.md. `commit` is synchronous (blocking git
    # subprocess + sync redis); async callers wrap it in asyncio.to_thread.
    # -----------------------------------------------------------------------

    @staticmethod
    def _norm_rel(rel: str) -> str:
        return rel.lstrip("/").replace("\\", "/")

    @staticmethod
    def safe_rel(rel: str) -> str:
        """Normalize a vault-relative path (strip leading '/', backslashes -> '/')
        and reject traversal ('..' segments). The single rel-path validator -
        supersedes write_gate._validate_rel_path."""
        rel = WikiDoc._norm_rel(rel)
        if not rel or ".." in rel.split("/"):
            raise ValueError(f"illegal vault path {rel!r}")
        return rel

    @staticmethod
    def _abs_checked(vault_id: str, rel: str) -> str:
        """Absolute on-disk path for a vault-relative doc path, traversal-checked."""
        rel = WikiDoc.safe_rel(rel)
        base = vault_abs_root(vault_id)
        abs_path = os.path.join(base, rel)
        WikiDoc.validate_path(abs_path, base)  # defense in depth
        return abs_path

    @staticmethod
    def _read_raw(abs_path: str) -> str:
        """The ONE document read open(): newline='' (no translation, CRLF
        survives) under DEFAULT_ENCODING."""
        with open(abs_path, "r", encoding=DEFAULT_ENCODING, newline="") as f:
            return f.read()

    @staticmethod
    def _write_raw(abs_path: str, out: str, errors: str = "xmlcharrefreplace") -> None:
        """The ONE document write open(): newline='' (verbatim, `out` already
        carries its intended EOL) under DEFAULT_ENCODING + errors policy."""
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding=DEFAULT_ENCODING, newline="",
                  errors=errors) as f:
            f.write(out)

    @staticmethod
    def read_text(vault_id: str, rel: str) -> tuple[str, str] | None:
        """Read a vault document. Returns (content_LF, eol) or None if absent.
        `content_LF` is normalized to '\\n'; `eol` is the file's detected line
        ending ('\\r\\n' or '\\n') - pass it back to write_text/commit to restore
        it. Reading via newline='' is what makes CRLF survive to here."""
        abs_path = WikiDoc._abs_checked(vault_id, rel)
        if not os.path.isfile(abs_path):
            return None
        raw = WikiDoc._read_raw(abs_path)
        eol = "\r\n" if "\r\n" in raw else "\n"
        return raw.replace("\r\n", "\n"), eol

    @staticmethod
    def read_text_at(abs_path: str) -> str:
        """LF-normalized content of a file at an ABSOLUTE path - for callers that
        hold a filesystem path rather than a (vault, rel) pair (RAG indexer,
        agent-file loader, bulk metadata scans). Uses the canonical low-level read
        (DEFAULT_ENCODING, newline=''); normalized to '\\n' because every consumer
        (frontmatter parsing, chunking, LLM input) assumes LF."""
        return WikiDoc._read_raw(abs_path).replace("\r\n", "\n")

    @staticmethod
    def _tracker(vault_id: str):
        """Git tracker for a vault. Lazy import: docversioning pulls in git
        subprocess plumbing that most WikiDoc callers never need."""
        from src.docversioning import MarkdownGitVersioning
        return MarkdownGitVersioning(vault_abs_root(vault_id))

    @staticmethod
    def read_text_at_revision(vault_id: str, rel: str, commit_sha: str) -> str | None:
        """LF-normalized content of a vault document as of a git commit, or None
        when the path did not exist at that commit (or the sha is unknown).

        The read-side counterpart to `commit` - the canonical way for anything in
        src/ to see history instead of the working tree. Returns a bare str, not
        read_text's (content, eol) pair: the historic blob's EOL is never
        replayed, because `commit` re-derives EOL from the file on disk.
        """
        from config import USE_GIT_VERSIONING
        if not (USE_GIT_VERSIONING and commit_sha):
            return None
        try:
            abs_path = WikiDoc._abs_checked(vault_id, rel)
            raw = WikiDoc._tracker(vault_id).get_file_at_commit(
                file_path=abs_path, commit_sha=commit_sha)
        except (FileNotFoundError, ValueError):
            return None
        return raw.replace("\r\n", "\n")

    @staticmethod
    def revision_info(vault_id: str, rel: str, commit_sha: str) -> dict:
        """Describe a revision of a document: short_sha, date_str, message and
        commits_since (commits touching `rel` between it and HEAD).

        Best-effort - returns {} when git can't answer, because callers use this
        for labels and warnings that must never break the operation they annotate.
        """
        from config import USE_GIT_VERSIONING
        if not (USE_GIT_VERSIONING and commit_sha):
            return {}
        try:
            abs_path = WikiDoc._abs_checked(vault_id, rel)
            return WikiDoc._tracker(vault_id).revision_info(abs_path, commit_sha)
        except (ValueError, OSError):
            return {}

    @staticmethod
    def write_text(vault_id: str, rel: str, content: str, *, eol: str = "\n",
                   errors: str = "xmlcharrefreplace") -> None:
        """Physical write of a vault document (no git, no debounce). `content`
        is normalized to LF first, then `eol` is applied, so an accidental
        '\\r\\n' in input can never become '\\r\\r\\n'."""
        abs_path = WikiDoc._abs_checked(vault_id, rel)
        lf = content.replace("\r\n", "\n")
        out = lf.replace("\n", "\r\n") if eol == "\r\n" else lf
        WikiDoc._write_raw(abs_path, out, errors=errors)

    @staticmethod
    def debounce_key(vault_id: str, rel: str) -> str:
        """The single git:debounce Redis key format. Shared by the setter
        (set_debounce) and the worker-side reader (task_definitions._git_debounced)
        so the two can never desync on the key shape."""
        return f"git:debounce:{vault_id}:{WikiDoc._norm_rel(rel)}"

    @staticmethod
    def write_bytes(vault_id: str, rel: str, data: bytes) -> None:
        """Traversal-checked binary write (attachments, chart PNGs, data files) -
        the binary sibling of write_text. safe_rel validation via _abs_checked, so
        an agent- or user-supplied path can't escape the vault. No git/debounce:
        binary attachments are RAG-excluded and versioned by the caller if at all."""
        abs_path = WikiDoc._abs_checked(vault_id, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as f:
            f.write(data)

    @staticmethod
    def set_debounce(vault_id: str, rel: str) -> None:
        """Set the git:debounce key. A write we just committed is ours; the file
        watcher must skip its own duplicate git commit (it still reindexes -
        wanted). 10s TTL matches the async reader the watcher chain uses."""
        from src.task_broker import get_sync_redis
        try:
            r = get_sync_redis()
            try:
                r.set(WikiDoc.debounce_key(vault_id, rel), "1", ex=10)
            finally:
                r.close()
        except Exception:
            pass

    @staticmethod
    def commit(vault_id: str, rel: str, content: str, *, message: str | None = None,
               checkpoint: bool = True, errors: str = "xmlcharrefreplace") -> None:
        """Versioned document mutation: checkpoint-before-mutate (commit the
        pre-image so the write is cleanly revertable - skipped for brand-new
        files) -> EOL-preserving write -> attributed git commit -> watcher
        debounce. The single entry point for mutating a vault document.

        EOL is read from the existing file here, so callers never thread it.
        Synchronous (git subprocess); async callers wrap in asyncio.to_thread.
        """
        from config import USE_GIT_VERSIONING
        pair = WikiDoc.read_text(vault_id, rel)
        eol = pair[1] if pair else "\n"
        existed = pair is not None
        rel_n = WikiDoc._norm_rel(rel)

        # Set the watcher's git:debounce FIRST - before ANY git op, including the
        # checkpoint commit below. A prior write to this file (e.g. its creation)
        # may already have an in-flight watcher git task; setting the key up front
        # makes that task (and the one the write below triggers) skip, so neither
        # races our save_version on the repo index lock (intermittent `git add`
        # exit 128). 10s TTL >> the fast checkpoint+write+commit that follows.
        WikiDoc.set_debounce(vault_id, rel_n)

        vt = None
        if USE_GIT_VERSIONING:
            from src import vault_registry
            from src.docversioning import MarkdownGitVersioning
            vault_registry.init_vault_repo(vault_id)
            vt = MarkdownGitVersioning(vault_abs_root(vault_id))
            abs_path = os.path.join(vault_abs_root(vault_id), rel_n)
            if checkpoint and existed:
                vt.save_version(abs_path, message=f"checkpoint before write: {rel_n}")

        WikiDoc.write_text(vault_id, rel, content, eol=eol, errors=errors)

        if vt is not None:
            abs_path = os.path.join(vault_abs_root(vault_id), rel_n)
            vt.save_version(abs_path, message=message)

    @staticmethod
    def delete_file(vault_id: str, rel: str, *, message: str | None = None,
                    checkpoint: bool = True) -> None:
        """Delete a vault document: checkpoint-before-delete (commit any pending
        edits so the removal is cleanly revertable) -> os.remove -> attributed
        git removal -> watcher debounce.

        Git operations are best-effort: a git failure logs but never blocks the
        actual file removal (a delete must not be held hostage by versioning; the
        watcher reconciles the DB regardless). No-op if the file is absent.
        """
        from config import USE_GIT_VERSIONING
        abs_path = WikiDoc._abs_checked(vault_id, rel)
        rel_n = WikiDoc._norm_rel(rel)
        if not os.path.isfile(abs_path):
            return
        vt = None
        if USE_GIT_VERSIONING:
            try:
                from src import vault_registry
                from src.docversioning import MarkdownGitVersioning
                vault_registry.init_vault_repo(vault_id)
                vt = MarkdownGitVersioning(vault_abs_root(vault_id))
                if checkpoint:
                    vt.save_version(abs_path, message=f"checkpoint before delete: {rel_n}")
            except Exception:
                logger.exception("delete_file: checkpoint/tracker failed %s:%s", vault_id, rel_n)
                vt = None
        os.remove(abs_path)
        if vt is not None:
            try:
                vt.remove_file(abs_path, message=message or f"Delete {rel_n}")
            except Exception:
                logger.exception("delete_file: git remove failed %s:%s", vault_id, rel_n)
        WikiDoc.set_debounce(vault_id, rel_n)

    @staticmethod
    def validate_path(file_path: str, base_dir: str) -> Path:
        """Resolve file_path and verify it's inside base_dir.

        Raises ValueError if the path would escape the base directory.
        Returns the resolved Path.
        """
        base = Path(base_dir).resolve()
        resolved = Path(file_path).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Path {file_path} escapes base directory {base_dir}")
        return resolved

    @staticmethod
    def _resolve_case_insensitive(file_path):
        """Find a file using case-insensitive filename matching.

        Checks the exact path first.  If that fails, scans the parent
        directory for a name that matches when lowercased.  This bridges
        the gap between case-insensitive Windows filesystems and the
        case-sensitive Linux filesystem inside Docker.

        Returns the actual path on disk if found, otherwise None.
        """
        if not file_path:
            return None
        p = Path(file_path)
        if p.exists():
            return file_path
        parent = p.parent
        if not parent.is_dir():
            return None
        target_lower = p.name.lower()
        for entry in parent.iterdir():
            if entry.name.lower() == target_lower:
                return str(entry)
        return None

    @staticmethod
    def _resolve_name_in_dir(dir_path, stem, allowed_exts=None):
        """Find the on-disk file matching a wikilink target inside dir_path.

        All SPACE_CONVERSION_ORDER characters are treated as one interchangeable
        "space" (via chunker.wikilink_key) and matching is case-insensitive, so
        [[Game Ideas]], [[Game_Ideas]] and a file "Game Ideas.md" all resolve to
        the same file -- and a mixed-separator file like "My_File Copy.md"
        resolves too. When several files share a canonical key, separator_rank
        (then an exact-case match) breaks the tie, honoring SPACE_CONVERSION_ORDER
        precedence. `allowed_exts` is a set of lowercased extensions (with the
        leading dot) to accept; None accepts any extension.

        Returns the actual path on disk, or None. This generalizes
        _resolve_case_insensitive: folding separators as well as case.
        """
        if not stem or not Path(dir_path).is_dir():
            return None
        target_key = wikilink_key(stem)
        matches = []
        for entry in Path(dir_path).iterdir():
            if not entry.is_file():
                continue
            entry_stem, entry_ext = os.path.splitext(entry.name)
            if allowed_exts is not None and entry_ext.lower() not in allowed_exts:
                continue
            if wikilink_key(entry_stem) == target_key:
                exact = 0 if entry_stem == stem else 1
                matches.append((separator_rank(entry_stem), exact, entry.name))
        if not matches:
            return None
        matches.sort()
        return str(Path(dir_path) / matches[0][2])

    @staticmethod
    def _frontmatter_span(content: str) -> tuple[int, int] | None:
        """(first YAML char, index of the closing `\\n---`), or None if absent.

        Accepts BOTH `---\\n` and `---\\r\\n` openers. Vault files are routinely CRLF
        and `_read_raw` preserves line endings by design, so a parser that only
        knows the LF opener silently reports "this document has no frontmatter" for
        every CRLF page - taking `Index: False` and friends with it.
        """
        if not content:
            return None
        for opener in ("---\n", "---\r\n"):
            if content.startswith(opener):
                close = content.find("\n---", len(opener))
                return None if close == -1 else (len(opener), close)
        return None

    @staticmethod
    def parse_frontmatter(content: str) -> dict[str, str]:
        """Parse YAML frontmatter delimited by --- into a dict.

        Returns empty dict for content without frontmatter.
        Supports YAML list values (inline [a, b] and block - item)
        which are normalized to comma-separated strings.
        """
        span = WikiDoc._frontmatter_span(content)
        if span is None:
            return {}
        start, close = span
        result = {}
        # Any trailing \r from a CRLF document is removed by the strip() calls below
        # (and by the block-list rule's own strip), so lines split on \n is enough.
        lines = content[start:close].split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()

                # YAML inline list: [a, b, c]
                if value.startswith("[") and value.endswith("]"):
                    value = value[1:-1].strip()

                # YAML block list: empty value followed by "  - item" lines
                elif value == "":
                    list_items = []
                    while i + 1 < len(lines):
                        m = re.match(r"^\s+-\s+(.+)", lines[i + 1])
                        if m:
                            list_items.append(m.group(1).strip())
                            i += 1
                        else:
                            break
                    if list_items:
                        value = ", ".join(list_items)

                result[key] = value
            i += 1
        return result

    @staticmethod
    def extract_manual_tags(tags_value: str) -> list[str]:
        """Split a Tags value string, return only !-prefixed tags."""
        tags = [t.strip() for t in tags_value.split(",") if t.strip()]
        return [t for t in tags if t.startswith("!")]

    @staticmethod
    def update_tags_in_content(content: str, new_auto_tags: list[str]) -> str:
        """Parse --- delimited frontmatter, find or insert the Tags: line,
        preserve !-prefixed manual tags, replace everything else with new_auto_tags.
        Return content unchanged if no --- frontmatter exists."""
        if not content.startswith("---\n"):
            return content

        close = content.find("\n---", 4)
        if close == -1:
            return content

        frontmatter = content[4:close]
        body = content[close + 4 :]

        lines = frontmatter.split("\n")
        tags_line_idx = None
        for i, line in enumerate(lines):
            if line.lower().startswith("tags:"):
                tags_line_idx = i
                break

        if tags_line_idx is not None:
            current_value = lines[tags_line_idx].split(":", 1)[1].strip()

            # Handle YAML inline list: tags: [a, b, c]
            if current_value.startswith("[") and current_value.endswith("]"):
                current_value = current_value[1:-1].strip()

            # Handle YAML block list: tags:\n  - a\n  - b
            elif current_value == "":
                list_items = []
                remove_start = tags_line_idx + 1
                remove_end = remove_start
                while remove_end < len(lines):
                    m = re.match(r"^\s+-\s+(.+)", lines[remove_end])
                    if m:
                        list_items.append(m.group(1).strip())
                        remove_end += 1
                    else:
                        break
                if list_items:
                    current_value = ", ".join(list_items)
                    del lines[remove_start:remove_end]

            manual_tags = WikiDoc.extract_manual_tags(current_value)
        else:
            manual_tags = []

        all_tags = manual_tags + [t for t in new_auto_tags if t not in manual_tags]
        new_tags_line = "Tags: " + ", ".join(all_tags)

        if tags_line_idx is not None:
            lines[tags_line_idx] = new_tags_line
        else:
            lines.append(new_tags_line)

        new_frontmatter = "---\n" + "\n".join(lines) + "\n---" + body
        return new_frontmatter

    @staticmethod
    def update_summary_in_content(content: str, summary: str) -> str:
        """Find or insert a Summary: line in frontmatter.
        Return content unchanged if no --- frontmatter exists."""
        if not content.startswith("---\n"):
            return content

        close = content.find("\n---", 4)
        if close == -1:
            return content

        frontmatter = content[4:close]
        body = content[close + 4 :]

        lines = frontmatter.split("\n")
        summary_line_idx = None
        for i, line in enumerate(lines):
            if line.lower().startswith("summary:"):
                summary_line_idx = i
                break

        # Clean summary: collapse to single line
        clean_summary = " ".join(summary.split())
        new_summary_line = "Summary: " + clean_summary

        if summary_line_idx is not None:
            lines[summary_line_idx] = new_summary_line
        else:
            lines.append(new_summary_line)

        return "---\n" + "\n".join(lines) + "\n---" + body

    @staticmethod
    def strip_frontmatter(content: str) -> str:
        """Return document body with frontmatter removed.

        The body is a SLICE of the original, so its line endings survive intact -
        see _frontmatter_span on why the delimiters must tolerate CRLF.
        """
        span = WikiDoc._frontmatter_span(content)
        if span is None:
            return content
        return content[span[1] + 4 :]

    @staticmethod
    def parse_url_path(path: str, default_page: str = DEFAULT_WIKI_PAGE):
        """Helper to break url into some commonly used component.
        
        Input a URL path or file path and outputs a dictionary
        of usefull variations. 
        
        Example Input:
        /wiki/notes/personal/birthdays.md
        
        Example Returns:
        {
            'path': 'notes/personal', 
            'path_list': ['notes', 'personal'], 
            'file_name': 'birthdays.md', 
            'file_ext': 'md', 
            'file_name_no_ext': 'birthdays', 
            'is_default_page_name': False
        }
        
        """
        is_default_page_name = False

        path = unquote(path)
        while ".." in path:
            path = path.replace("..", "")
        while "\\" in path:
            path = path.replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        path_split = path.split("/")
        path_split = [each for each in path_split if each]

        if path_split and path_split[0] in RESERVED_PATHS:
            path_split.pop(0)

        if path_split:
            file_name = path_split.pop()
            if len(path_split) == 0:
                path_split = [""]
            file_name_parts = file_name.split(".")
            if len(file_name_parts) > 1:
                if file_name_parts[0]:
                    file_ext = file_name_parts.pop().lower()
                    file_name_no_ext = file_name[: -1 - len(file_ext)]
                else:
                    # could be .hidden.md  ['', 'hidden', 'md']
                    # or .hidden  ['', 'hidden']
                    if len(file_name_parts) == 2:
                        file_ext = ""
                        file_name_no_ext = file_name
                    else:
                        file_ext = file_name_parts.pop().lower()
                        file_name_no_ext = ".".join(file_name_parts)
            else:
                file_ext = ""
                file_name_no_ext = file_name
        else:
            # Empty path -> the start page. Which page that is, is per-vault
            # (vault_registry.vault_default_page); the caller supplies it, so this
            # parser stays vault-agnostic for its handful of vault-less callers.
            path_split = [""]
            file_name = default_page
            file_ext = ""
            file_name_no_ext = default_page
            is_default_page_name = True

        response = {
            "path": "/".join(path_split),
            "path_list": path_split,
            "file_name": file_name,
            "file_ext": file_ext,
            "file_name_no_ext": file_name_no_ext,
            "is_default_page_name": is_default_page_name,
        }
        return response

    @staticmethod
    def markdown_page_name(url_pieces):
        """helper for cleaner urls to file.md files"""
        path = url_pieces["path"]
        path_list = url_pieces["path_list"]
        file_name = url_pieces["file_name"]
        file_ext = url_pieces["file_ext"]
        file_name_base = url_pieces["file_name_no_ext"]

        if file_ext == "":
            page_name = file_name_base
        elif file_ext == "md":
            page_name = file_name_base
        else:
            page_name = file_name

        return page_name

    @staticmethod
    def resolve_wikilink(target, source_dir, vault=DEFAULT_VAULT):
        """Resolve a wikilink target to a real vault file, Obsidian-style.

        Called by the renderer (MarkdownDocTransform -> WikiLinkExtension) as the
        resolve_callback. ``target`` is the link's page part (anchor/alias already
        stripped); ``source_dir`` is the current document's folder, used to break
        ties by proximity. Resolution is scoped to ``vault`` so a link never crosses a
        vault boundary. Returns the target's vault-relative path (with .md) or None.
        The renderer binds ``vault`` via functools.partial from the document's vault."""
        from src.vault_index import resolve

        return resolve(target, source_dir, vault)

    @staticmethod
    def wikilink_page_check(resolved_name, vault=DEFAULT_VAULT):
        """
        check if a wiki link points to an actual document

        Called by MardownDocTransform.get_content -> WikiLinkExtension. ``vault`` scopes
        the existence check so a link only resolves within its own vault.
        """

        path = PurePosixPath(resolved_name)
        parts = []
        for part in path.parts:
            if part == "..":
                if parts and parts[-1] != "..":
                    parts.pop()
                else:
                    parts.append(part)
            elif part == "/":
                pass
            elif part != ".":
                parts.append(part)
        resolved_path = "/".join(parts)

        # resolved_path = path.resolve()
        url_pieces = WikiDoc.parse_url_path(resolved_path)
        file_exists = WikiDoc.markdown_file_exists(url_pieces, any_type=True, vault=vault)

        if not file_exists:
            return False
        return True

    @staticmethod
    def markdown_file_exists(url_pieces, any_type=False, vault=DEFAULT_VAULT):
        """Determines if a markdown url exists as a file.

        Resolution folds SPACE_CONVERSION_ORDER separators and case (see
        _resolve_name_in_dir), so it shares one code path with _test_existence.
        Returns the on-disk path if found, else False.
        """
        path_list = url_pieces["path_list"]
        file_ext = url_pieces["file_ext"]
        stem = url_pieces["file_name_no_ext"]

        if file_ext in ("", "md"):
            allowed_exts = {".md"}
        elif any_type:
            # match the requested extension if given, else any extension
            allowed_exts = {"." + file_ext} if file_ext else None
        else:
            return False

        wiki_dir = os.path.join(vault_root(vault), *path_list)
        resolved = WikiDoc._resolve_name_in_dir(wiki_dir, stem, allowed_exts)
        return resolved if resolved else False
