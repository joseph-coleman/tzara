# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import html
import logging
import re
import xml.etree.ElementTree as etree
from urllib.parse import quote

logger = logging.getLogger(__name__)

from markdown.extensions import Extension
from markdown.blockprocessors import BlockProcessor
from markdown.inlinepatterns import InlineProcessor
from markdown.preprocessors import Preprocessor
from markdown.treeprocessors import Treeprocessor

from config import PREVIEW_EMBED_FILE_TYPES
from src.md_sections import strip_comment_blocks


def _no_resolver(target, source_dir):
    """Sentinel default for the wikilink resolve_callback meaning 'none wired'.

    Must be a function, not None: python-markdown's Extension config coerces a None
    default through parseBoolValue (which would turn a real callable into True), but
    leaves a function default untouched -- the same reason page_exists_callback's
    default is a lambda. The processor compares against this by identity."""
    return None


def normalize_anchor(anchor: str) -> str:
    """Normalize anchor like the Python Markdown TOC extension."""
    anchor = anchor.lower()
    # Replace spaces with dash
    anchor = re.sub(r"\s+", "-", anchor)
    # Remove all punctuation except letters, numbers, dash
    anchor = re.sub(r"[^a-z0-9\-]", "", anchor)
    # Collapse multiple dashes
    anchor = re.sub(r"-+", "-", anchor)
    return anchor.strip("-")


class WikiLinkInlineProcessor(InlineProcessor):
    def __init__(self, pattern: str, config):
        super().__init__(pattern)
        self.base_url = config["base_url"].rstrip("/")
        self.edit_url = config["edit_url"].rstrip("/")
        self.current_path = config["current_path"]  # e.g. "docs/Install_Guide"
        self.page_exists_callback = config["page_exists_callback"]
        # Obsidian-style resolver: (target, source_dir) -> resolved vault-relative
        # path or None. When wired it replaces resolve_page_name + the existence
        # callback; when None (other callers) the legacy folder-relative path is used.
        self.resolve_callback = config.get("resolve_callback")

    def resolve_page_name(self, page_name: str) -> str:
        """Resolve page names with relative/absolute rules."""
        resolved = page_name.strip()

        resolved = resolved.rstrip("/")

        # print(f"{self.current_path=}")

        if resolved.startswith("/"):
            resolved = resolved.lstrip("/")
            # print("@@@@ path A", resolved)
        elif self.current_path:
            resolved = "/".join([self.current_path, resolved])
            # print("$$$$ path B", resolved)
        # else:
        #     resolved = resolved
        return resolved

    def default_link_text(self, page_name: str, resolved_name: str) -> str:
        """Choose a default display text if user didn't specify one."""
        if "#" in page_name:
            page_name = page_name.split("#", 1)[0]  # drop anchor
        if "/" in page_name or page_name.startswith("."):
            return page_name.split("/")[-1]
        return page_name

    def handleMatch(self, m, data):
        raw_text = m.group(1).strip()

        # print("=============")
        # print(f"{raw_text=}")

        # """
        # Ok, what do we want to do here.
        # a wikilink might contain a # anchor, so we filter that stuff out
        # [[/my page/doc#hello world]]
        # And also some display text using pipe notation [[/my_page|Hello World]]
        # Also, filter that out.  The page name is the important part for creating
        # something to link to.

        # We don't really place any restrictions on file names that can be created.
        # So far.  So why place restrictions on wiki links?

        # Of course, it would be nice to have sanitized file structure,
        # but other than ., .., and / or \, we should be fine.  In the
        # case of an illegal character being used, we should simply fail instead
        # of trying to catch it.

        # Two main types of page links to conisder, absolute and relative
        # Absolute starts with a /, relative does not.

        # A wiki page that ends with a slash / doesn't have a document name,
        # and we don't really create one

        # URL encodings could be a problem.  On disk, I can create the following
        # `hello world.md`, `hello+world.md` and `hello%20world.md`, and they're
        # all unique, but they're all the "same" in terms of a URL.  Obsidian has
        # no problems with these files, but a web interface necessarily makes some
        # of these unreachable.  This requires an opinion!

        # """

        # Pipe syntax [[Page|Text]]
        if "|" in raw_text:
            page_part, link_text = [p.strip() for p in raw_text.split("|", 1)]
        else:
            page_part, link_text = raw_text, None

        # for the examples on scratch, the link text is all None because I'm not
        # using the | pipe syntax.
        # print(f"{page_part=}, {link_text=}")

        # remove anchor if present
        if "#" in page_part:
            page_name, anchor = page_part.split("#", 1)
            normalized_anchor = normalize_anchor(anchor)
        else:
            page_name, anchor = page_part, None
            normalized_anchor = None

        # Resolve the link to a real vault file the way Obsidian does: a vault-global
        # match by basename or path-suffix, with proximity to the current document
        # breaking ties. The resolver returns the target's vault-relative path, or
        # None when nothing matches. When unresolved (or no resolver is wired) we keep
        # the legacy folder-relative target so clicking a red link still creates the
        # page where the author implied it.
        if self.resolve_callback is not _no_resolver:
            resolved_rel = self.resolve_callback(page_name, self.current_path)
            if resolved_rel is not None:
                page_path = resolved_rel[:-3] if resolved_rel.endswith(".md") else resolved_rel
                missing = False
            else:
                page_path = self.resolve_page_name(page_name)
                missing = True
        else:
            page_path = self.resolve_page_name(page_name)
            missing = self.page_exists_callback is not None and not self.page_exists_callback(page_path)

        # Preserve the resolved on-disk path. The old normalize_page_name folded
        # spaces to underscores, but folder segments must match the filesystem
        # exactly (only the file basename resolves separator-insensitively, see
        # wikidoc._resolve_name_in_dir), so a real "A Folder/" was unreachable as
        # "/wiki/A_Folder/...". quote() yields a valid href; the browser/Starlette
        # round-trip it back to the real characters (Starlette decodes path params,
        # and parse_url_path unquote()s on the way in).
        url = f"{self.base_url}/{quote(page_path, safe='/')}"
        if anchor:
            url += f"#{normalized_anchor}"

        # Default link text
        if link_text is None:
            link_text = self.default_link_text(page_part, page_path)

        el = etree.Element("a")
        el.set("href", url)
        el.text = link_text
        el.set("class", "wikilink")

        if anchor:
            el.set("title", f"{anchor}")

        if missing:
            el.set("class", "missing")
            # url[len(self.base_url):] strips the "/wiki" prefix as a prefix.
            # (str.lstrip would strip a *character set*, mangling paths that
            # start with w/i/k chars, e.g. "/wiki/ideas" -> "deas".)
            el.set("href", f"{self.edit_url}{url[len(self.base_url):]}")

        return el, m.start(0), m.end(0)


class WikiLinkExtension(Extension):
    def __init__(self, **kwargs):
        # specify defaults first.
        self.config = {
            "base_url": ["/wiki", "Base URL for wiki links"],
            "edit_url": ["/edit", "Base URL for edit links"],
            "current_path": [
                "/",
                "Current page path, for relative resolution",
            ],
            "page_exists_callback": [
                lambda x: True,
                "Function to check if a page exists",
            ],
            "resolve_callback": [
                _no_resolver,
                "Obsidian-style resolver (target, source_dir) -> vault-rel path|None",
            ],
        }
        # this super sets the config parameters and overwrites the defaults.
        super().__init__(**kwargs)

    def extendMarkdown(self, md):

        WIKI_LINK_RE = r"\[\[([^\]]+)\]\]"  # matches [[Page Name]]
        md.inlinePatterns.register(
            WikiLinkInlineProcessor(
                WIKI_LINK_RE,
                config=self.getConfigs(),
            ),
            "wikilink",
            175,
        )


class ImageEmbedInlineProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        src = m.group(1)
        img = etree.Element("img")
        img.set("src", src)
        img.set("alt", "")
        return img, m.start(0), m.end(0)


class ImageEmbedExtension(Extension):
    def extendMarkdown(self, md):
        IMAGE_EMBED_RE = r"!\[\[([^\]]+)\]\]"  # Matches ![[filename]]
        md.inlinePatterns.register(
            ImageEmbedInlineProcessor(IMAGE_EMBED_RE, md), "image_embed", 175
        )


class CanvasEmbedPreprocessor(Preprocessor):
    """Turn a full-line ``![[Board.canvas]]`` embed into a placeholder <div> that
    the page template hydrates into a read-only TzaraCanvas.

    Runs as a Preprocessor (before block parsing) for two reasons: it can emit a
    block-level <div> via htmlStash without it being wrapped in a <p> (the fate of
    an inline <div>, which the browser then silently splits), and it runs before
    the inline ``![[...]]`` image handler so it can claim ``.canvas`` targets and
    leave every other embed untouched. Only whole-line matches are handled -- an
    embedded canvas is a block, never an inline fragment. Mirrors the flag-and-
    conditional-script flow MermaidExtension uses (see base.html)."""

    # Whole-line embed only. Height is an optional ``|N`` suffix, Obsidian-style.
    RE = re.compile(r"^[ \t]*!\[\[([^\]\n]+?)\]\][ \t]*$", re.MULTILINE)

    def __init__(self, md, config):
        super().__init__(md)
        self.vault = config["vault"]
        self.current_path = config["current_path"]
        self.resolve_callback = config["resolve_callback"]
        self.default_height = int(config["default_height"])

    def run(self, lines):
        text = "\n".join(lines)

        def repl(m):
            inner = m.group(1).strip()
            # Optional |height suffix (px). Obsidian reuses | for alias text, but on
            # a canvas embed a bare integer reads unambiguously as a height.
            height = self.default_height
            path_part = inner
            if "|" in inner:
                path_part, _, tail = inner.partition("|")
                path_part = path_part.strip()
                tail = tail.strip()
                if tail.isdigit():
                    height = int(tail)

            if not path_part.lower().endswith(".canvas"):
                # Not a canvas: leave the line verbatim for the inline image embed.
                return m.group(0)

            # Resolve to a real vault file the same way wikilinks do (vault-global
            # basename/suffix match, proximity tie-break). Fall back to the literal
            # target so a broken embed still renders an (empty) canvas card.
            resolved = None
            if self.resolve_callback is not _no_resolver:
                resolved = self.resolve_callback(path_part, self.current_path)
            rel = resolved or path_part.lstrip("/")

            # /raw serves the canvas JSON for the fetch; /wiki opens the full editor.
            src = f"/raw/{self.vault}/{quote(rel, safe='/')}"
            href = f"/wiki/{self.vault}/{quote(rel, safe='/')}"
            title = html.escape(rel.split("/")[-1][: -len(".canvas")])

            self.md.tzara_has_canvas_embed = True
            snippet = (
                '<div class="canvas-embed">'
                '<div class="canvas-embed-bar">'
                f'<span class="canvas-embed-title">{title}</span>'
                f'<a class="canvas-embed-open" href="{html.escape(href)}" '
                'title="Open canvas">&#10530; open</a>'
                '</div>'
                f'<div class="canvas-embed-mount" data-canvas-src="{html.escape(src)}" '
                f'style="height:{height}px"></div>'
                '</div>'
            )
            return self.md.htmlStash.store(snippet)

        return self.RE.sub(repl, text).split("\n")


class CanvasEmbedExtension(Extension):
    def __init__(self, **kwargs):
        self.config = {
            "vault": ["main", "Vault id for building /raw and /wiki URLs"],
            "current_path": ["", "Source doc path, for proximity tie-breaks"],
            "resolve_callback": [
                _no_resolver,
                "Obsidian-style resolver (target, source_dir) -> vault-rel path|None",
            ],
            "default_height": [420, "Default embed height in px when |N is omitted"],
        }
        super().__init__(**kwargs)

    def extendMarkdown(self, md):
        md.tzara_has_canvas_embed = False
        md.registerExtension(self)
        # Priority 26: a Preprocessor, so it runs before the inline image embed;
        # sits alongside mermaid (27) with which it never overlaps.
        md.preprocessors.register(
            CanvasEmbedPreprocessor(md, self.getConfigs()), "canvas_embed", 26
        )


class FileEmbedPreprocessor(Preprocessor):
    """Render a full-line ``![[data.csv]]`` / ``![[report.pdf]]`` embed as an
    inline preview card, for browsing (not the chat agent).

    Same shape as CanvasEmbedPreprocessor - a Preprocessor so it can emit a
    block-level card via htmlStash and run before the inline ``![[...]]`` image
    handler - but it claims only the file types it knows and leaves every other
    embed (images, canvas) verbatim for their own handlers. CSV/TSV are read
    server-side with a bounded row cap, so a large data file sends only the
    preview rows to the browser, never the whole file. PDF is a static iframe.
    """

    RE = re.compile(r"^[ \t]*!\[\[([^\]\n]+?)\]\][ \t]*$", re.MULTILINE)

    def __init__(self, md, config):
        super().__init__(md)
        self.vault = config["vault"]
        self.current_path = config["current_path"]
        self.resolve_callback = config["resolve_callback"]
        self.csv_max_rows = int(config["csv_max_rows"])
        self.csv_max_cols = int(config["csv_max_cols"])
        self.pdf_height = int(config["pdf_height"])

    def run(self, lines):
        text = "\n".join(lines)

        def repl(m):
            inner = m.group(1).strip()
            path_part = inner
            height = self.pdf_height
            if "|" in inner:
                path_part, _, tail = inner.partition("|")
                path_part = path_part.strip()
                tail = tail.strip()
                if tail.isdigit():
                    height = int(tail)

            ext = path_part.rsplit(".", 1)[-1].lower() if "." in path_part else ""
            if ext not in PREVIEW_EMBED_FILE_TYPES:
                # Not ours - leave for the canvas/image embed handlers.
                return m.group(0)

            resolved = None
            if self.resolve_callback is not _no_resolver:
                resolved = self.resolve_callback(path_part, self.current_path)
            rel = resolved or path_part.lstrip("/")

            href = f"/raw/{self.vault}/{quote(rel, safe='/')}"
            title = html.escape(rel.split("/")[-1])

            if ext == "pdf":
                snippet = _pdf_embed_card(href, title, height)
            else:
                snippet = _csv_embed_card(
                    href, title, self.vault, rel,
                    delimiter="\t" if ext == "tsv" else ",",
                    max_rows=self.csv_max_rows, max_cols=self.csv_max_cols,
                )
            return self.md.htmlStash.store(snippet)

        return self.RE.sub(repl, text).split("\n")


class FileEmbedExtension(Extension):
    def __init__(self, **kwargs):
        self.config = {
            "vault": ["main", "Vault id for building /raw URLs"],
            "current_path": ["", "Source doc path, for proximity tie-breaks"],
            "resolve_callback": [
                _no_resolver,
                "Obsidian-style resolver (target, source_dir) -> vault-rel path|None",
            ],
            "csv_max_rows": [50, "Max data rows rendered in a CSV/TSV preview"],
            "csv_max_cols": [25, "Max columns rendered in a CSV/TSV preview"],
            "pdf_height": [720, "PDF embed height in px when |N is omitted"],
        }
        super().__init__(**kwargs)

    def extendMarkdown(self, md):
        md.registerExtension(self)
        # Priority 26: alongside canvas_embed, before the inline image embed. Each
        # whole-line embed preprocessor skips the types it doesn't own, so their
        # relative order is immaterial.
        md.preprocessors.register(
            FileEmbedPreprocessor(md, self.getConfigs()), "file_embed", 26
        )


def _no_render(text, *, current_path, depth, visited):
    """Sentinel default for the nested-render callback (see _no_resolver). When
    no renderer is wired the fragment can't be rendered, so escape it as text.
    Returns (html, flags) to match render_markdown_fragment's contract."""
    return html.escape(text), {}


def _transclusion_notice(kind, label):
    """A small inline notice for a doc embed that couldn't be inlined (missing
    page/section, cycle, or depth cap). Static HTML so it survives htmlStash
    without being re-parsed - a broken include stays visible, never silent."""
    return (
        f'<div class="md-transclusion md-transclusion-notice" data-kind="{kind}">'
        f'<span class="md-transclusion-notice-icon">&#9888;</span> '
        f'{html.escape(label)}'
        f'</div>'
    )


def _extract_section(body, anchor):
    """Slice a section or block out of `body` for an ``![[Page#anchor]]`` embed.

    - ``#Heading`` -> from the first ATX heading whose text folds to ``anchor``
      (TOC-style, via ``normalize_anchor``) through the line before the next
      heading of the same-or-higher level.
    - ``^blockid`` -> the block (run of non-blank lines) ending with the trailing
      `` ^blockid`` marker; the marker itself is stripped from the output.

    Code fences are skipped so a ``#`` or ``^id`` inside a ``` block never
    matches. Addressing and slicing come from ``src.md_sections`` - the same
    parser chat's section tools and the agent capabilities use - so a heading
    means the same thing everywhere. Returns the slice, or None if not found."""
    from src.md_sections import extract_block_ref, parse_sections, section_slice

    # --- block reference: ![[Page#^blockid]] ---
    if anchor.startswith("^"):
        return extract_block_ref(body, anchor[1:])

    # --- heading section: ![[Page#Heading]] ---
    # Match TOC-style (normalize_anchor), first heading wins; the returned slice
    # INCLUDES the heading line and runs to the next same-or-higher heading.
    want = normalize_anchor(anchor)
    for section in parse_sections(body):
        if section["level"] == 0:
            continue
        if normalize_anchor(section["heading_text"]) == want:
            return section_slice(body, section).rstrip()
    return None


def render_transclusion(target, anchor, *, vault, current_path, resolve_callback,
                        render_callback, depth, visited, max_depth, host_md=None):
    """Resolve, read, optionally slice, and render one ``![[Page]]`` document
    embed into a framed container. Returns self-contained HTML (safe to
    htmlStash). The depth cap, cycle detection, and a missing page/section each
    produce a visible inline notice rather than a silently-dropped embed.

    ``host_md`` is the embedding page's markdown instance; the embedded page's
    client-side feature flags (mermaid/jupyter/latex/canvas) are OR'd into it so
    the host template loads the assets those features need to activate."""
    label = target + (f"#{anchor}" if anchor else "")

    if depth >= max_depth:
        return _transclusion_notice(
            "depth", f"Include depth limit reached ({max_depth}): ![[{label}]]"
        )

    resolved = None
    if resolve_callback is not _no_resolver:
        resolved = resolve_callback(target, current_path)
    if not resolved:
        return _transclusion_notice("missing", f"Page not found: ![[{label}]]")

    if resolved in visited:
        return _transclusion_notice("cycle", f"Circular include: ![[{label}]]")

    from src.wikidoc import WikiDoc

    read = WikiDoc.read_text(vault, resolved)
    if read is None:
        return _transclusion_notice("missing", f"Page not found: ![[{label}]]")
    content, _eol = read
    body = WikiDoc.strip_frontmatter(content)

    if anchor:
        section = _extract_section(body, anchor)
        if section is None:
            return _transclusion_notice("missing", f"Section not found: ![[{label}]]")
        body = section

    # Nested render: same extension set, one level deeper, this page marked
    # visited so a descendant can't re-include an ancestor. The child's source
    # path is passed WITHOUT the .md suffix to match the top-level convention
    # (url_pieces["path"]) that proximity resolution expects - _key_segments
    # folds .md away, so this is defensive consistency, not a behavior change.
    child_source = resolved[:-3] if resolved.lower().endswith(".md") else resolved
    child_html, child_flags = render_callback(
        body, current_path=child_source, depth=depth + 1, visited=visited | {resolved}
    )
    # Bubble the embedded page's client-side feature flags up to the host md so
    # base.html loads mermaid/jupyter/latex/canvas assets for embedded content.
    if host_md is not None:
        for flag, on in child_flags.items():
            if on:
                setattr(host_md, flag, True)

    # Title bar: child frontmatter `title`, else the filename stem; links to the
    # source page. quote() the href like the canvas/file embeds do.
    fm = WikiDoc.parse_frontmatter(content)
    stem = resolved.rsplit("/", 1)[-1]
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    title = fm.get("title") or stem
    link_rel = resolved[:-3] if resolved.lower().endswith(".md") else resolved
    href = f"/wiki/{vault}/{quote(link_rel, safe='/')}"
    title_text = html.escape(title) + (
        f" &#8250; {html.escape(anchor)}" if anchor else ""
    )

    return (
        f'<div class="md-transclusion" data-src="{html.escape(resolved)}">'
        f'<div class="md-transclusion-title">'
        f'<a href="{html.escape(href)}">{title_text}</a>'
        f'</div>'
        f'<div class="md-transclusion-body">{child_html}</div>'
        f'</div>'
    )


class TranscludeEmbedPreprocessor(Preprocessor):
    """Render a full-line ``![[Page]]`` / ``![[Page#Heading]]`` / ``![[Page#^blk]]``
    embed of a *markdown* document as an inline, framed include (transclusion).

    Same shape as CanvasEmbedPreprocessor/FileEmbedPreprocessor - a whole-line
    Preprocessor that stashes block HTML and runs before the inline ``![[...]]``
    image handler. It claims only markdown targets (no extension or ``.md``) and
    leaves images/canvas/csv/tsv/pdf verbatim for their own handlers. The
    included page is rendered independently (its own markdown pass) and dropped
    into a container, so nested includes, mermaid, jupyter, wikilinks etc. all
    work inside an embed; recursion is bounded by max_depth + a visited-set."""

    RE = re.compile(r"^[ \t]*!\[\[([^\]\n]+?)\]\][ \t]*$", re.MULTILINE)

    def __init__(self, md, config):
        super().__init__(md)
        self.vault = config["vault"]
        self.current_path = config["current_path"]
        self.resolve_callback = config["resolve_callback"]
        self.render_callback = config["render_callback"]
        self.depth = int(config["depth"])
        self.visited = frozenset(config["visited"])
        self.max_depth = int(config["max_depth"])

    def run(self, lines):
        text = "\n".join(lines)

        def repl(m):
            inner = m.group(1).strip()
            # Drop an optional |alias/size suffix; a doc embed ignores it, but
            # ![[Page|x]] should still resolve to Page.
            path_part = inner.split("|", 1)[0].strip()
            target, _, anchor = path_part.partition("#")
            target = target.strip()
            anchor = anchor.strip()

            last_seg = target.rsplit("/", 1)[-1]
            ext = last_seg.rsplit(".", 1)[-1].lower() if "." in last_seg else ""
            if ext not in ("", "md"):
                # Not a markdown doc: leave for canvas/file/image handlers.
                return m.group(0)

            self.md.tzara_has_transclusion = True
            try:
                snippet = render_transclusion(
                    target, anchor,
                    vault=self.vault,
                    current_path=self.current_path,
                    resolve_callback=self.resolve_callback,
                    render_callback=self.render_callback,
                    depth=self.depth,
                    visited=self.visited,
                    max_depth=self.max_depth,
                    host_md=self.md,
                )
            except Exception:
                # A broken embedded page must never take down the host render -
                # degrade to a visible notice, matching the missing/cycle paths.
                label = target + (f"#{anchor}" if anchor else "")
                logger.exception("transclusion render failed for ![[%s]]", label)
                snippet = _transclusion_notice(
                    "error", f"Could not include ![[{label}]]"
                )
            return self.md.htmlStash.store(snippet)

        return self.RE.sub(repl, text).split("\n")


class TranscludeEmbedExtension(Extension):
    def __init__(self, **kwargs):
        self.config = {
            "vault": ["main", "Vault id for building /wiki URLs and reads"],
            "current_path": ["", "Source doc path, for proximity tie-breaks"],
            "resolve_callback": [
                _no_resolver,
                "Obsidian-style resolver (target, source_dir) -> vault-rel path|None",
            ],
            "render_callback": [
                _no_render,
                "Nested renderer (text, *, current_path, depth, visited) -> html",
            ],
            "depth": [0, "Current include depth (0 at the top-level page)"],
            "visited": [frozenset(), "Vault-rel paths already on the include stack"],
            "max_depth": [3, "Max include nesting before a notice is shown"],
        }
        super().__init__(**kwargs)

    def extendMarkdown(self, md):
        md.tzara_has_transclusion = False
        md.registerExtension(self)
        # Priority 26: a Preprocessor alongside canvas_embed/file_embed, before
        # the inline image embed (175). Each whole-line embed preprocessor skips
        # the types it doesn't own, so their relative order is immaterial.
        md.preprocessors.register(
            TranscludeEmbedPreprocessor(md, self.getConfigs()), "transclude_embed", 26
        )


def _vault_abs_file(vault, rel):
    """Absolute on-disk path for a vault-relative file, or None if it escapes the
    vault root (defense against ``../`` traversal in an embed target)."""
    import os
    from config import vault_abs_root

    root = os.path.realpath(vault_abs_root(vault))
    ap = os.path.realpath(os.path.join(root, rel))
    if root != ap and os.path.commonpath([root, ap]) != root:
        return None
    return ap


def _csv_embed_card(href, title, vault, rel, *, delimiter, max_rows, max_cols):
    """Server-rendered CSV/TSV preview card. First row is treated as the header.
    Reads at most max_rows+1 rows so a huge file isn't loaded to preview it."""
    import csv as _csv

    bar = (
        '<div class="file-embed-bar">'
        f'<span class="file-embed-title">&#128202; {title}</span>'
        f'<a class="file-embed-open" href="{href}" download title="Download">'
        '&#10515; download</a>'
        '</div>'
    )

    abs_path = _vault_abs_file(vault, rel)
    if not abs_path:
        return (
            '<div class="file-embed csv-embed">' + bar +
            '<div class="file-embed-error">Could not locate this file.</div></div>'
        )

    rows = []
    truncated = False
    try:
        with open(abs_path, "r", encoding="utf-8-sig", errors="replace",
                  newline="") as f:
            reader = _csv.reader(f, delimiter=delimiter)
            for i, row in enumerate(reader):
                if i > max_rows:  # header + max_rows data rows already collected
                    truncated = True
                    break
                rows.append(row[:max_cols])
    except OSError:
        return (
            '<div class="file-embed csv-embed">' + bar +
            '<div class="file-embed-error">Could not read this file.</div></div>'
        )

    if not rows:
        return (
            '<div class="file-embed csv-embed">' + bar +
            '<div class="file-embed-error">This file is empty.</div></div>'
        )

    def _cell(v):
        v = str(v)
        if len(v) > 300:
            v = v[:300] + "…"
        return html.escape(v)

    header, body = rows[0], rows[1:]
    thead = "<thead><tr>" + "".join(f"<th>{_cell(c)}</th>" for c in header) + "</tr></thead>"
    trs = "".join(
        "<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in r) + "</tr>" for r in body
    )
    note = f"Showing first {len(body)} row{'' if len(body) == 1 else 's'}"
    note += " (truncated)" if truncated else ""

    return (
        '<div class="file-embed csv-embed">' + bar +
        '<div class="file-embed-body csv-embed-body">'
        f'<table class="csv-embed-table">{thead}<tbody>{trs}</tbody></table>'
        '</div>'
        f'<div class="file-embed-note">{note}</div>'
        '</div>'
    )


def _pdf_embed_card(href, title, height):
    """Static PDF viewer card - a plain <iframe> at /raw, no JS."""
    return (
        '<div class="file-embed pdf-embed">'
        '<div class="file-embed-bar">'
        f'<span class="file-embed-title">&#128196; {title}</span>'
        f'<a class="file-embed-open" href="{href}" target="_blank" '
        'rel="noopener" title="Open PDF">&#10530; open</a>'
        '</div>'
        f'<iframe class="pdf-embed-frame" src="{href}#view=FitH" '
        f'style="height:{int(height)}px" title="{title}" loading="lazy"></iframe>'
        '</div>'
    )


class StrikeThroughInlineProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        strike_through = etree.Element("s")
        strike_through.text = m.group(1)
        return strike_through, m.start(0), m.end(0)


class StrikeThroughExtension(Extension):
    def extendMarkdown(self, md):
        MD_RE = r"~~(.*?)~~"
        md.inlinePatterns.register(
            StrikeThroughInlineProcessor(MD_RE, md), "strikethrough", 175
        )


# obsidian style comments
#
# `%%` comments come in two shapes and they need two different layers:
#
#   inline: `text %%hidden%% text`   - both markers land inside one block
#   block:  a bare `%%` on its own line, everything up to the next bare `%%`
#
# The inline pattern below handles the first. The second CANNOT be done with an
# inline pattern at all: Python-Markdown splits the document into blocks BEFORE
# inline patterns run, so a comment containing a blank line, a heading, a rule -
# anything the block parser claims - is torn into separate blocks and no regex
# can span them. The `%%` markers then survive as visible literal text. Obsidian
# supports block comments spanning multiple paragraphs, so the block form is
# stripped up front by a Preprocessor instead (matching what chunker.py already
# does line-wise for RAG).
class ObsidianCommentProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        obsidian_comment = etree.Element("span")
        obsidian_comment.text = ""
        return obsidian_comment, m.start(0), m.end(0)


class ObsidianCommentPreprocessor(Preprocessor):
    """
    Remove whole-line `%%` ... `%%` comment blocks before block parsing.

    Registered at priority 28: after frontmatter (50) so the YAML header is
    already gone, but ahead of jupyter_cell/mermaid (27), the embed
    preprocessors (26) and fenced_code (25). Commenting out a Jupyter cell or an
    `![[embed]]` has to mean it never runs or renders, not that it renders and
    is hidden afterwards.

    The rule itself lives in md_sections.strip_comment_blocks, shared with the
    RAG chunker: what the reader cannot see and what the index cannot find have
    to be the same set of lines, or the rendered page stops being an honest
    report of how the system read the document.
    """

    def run(self, lines):
        return strip_comment_blocks("\n".join(lines)).split("\n")


class ObsidianCommentExtension(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.register(
            ObsidianCommentPreprocessor(md), "obsidian_comment_block", 28
        )
        # the begginning (?s) is to catch multiline comments.
        MD_RE = r"(?s)%%.*?%%"
        md.inlinePatterns.register(
            ObsidianCommentProcessor(MD_RE, md), "obsidian_comment", 175
        )


class HighLightInlineProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        highlight_mark = etree.Element("mark")
        highlight_mark.text = m.group(1)
        return highlight_mark, m.start(0), m.end(0)


class HighLightExtension(Extension):
    def extendMarkdown(self, md):
        MD_RE = r"==(.*?)=="
        md.inlinePatterns.register(
            HighLightInlineProcessor(MD_RE, md), "highlightinline", 175
        )


class AutoLinkInlineProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        url = m.group(1)
        el = etree.Element("a")
        el.set("href", url)
        el.text = url
        el.set("target", "_blank")  # open in a rnew tab
        return el, m.start(0), m.end(0)


class AutoLinkExtension(Extension):
    def extendMarkdown(self, md):
        URL_RE = r"(https?://[^\s<]+)"
        md.inlinePatterns.register(AutoLinkInlineProcessor(URL_RE, md), "autolink", 200)


class UnifiedMathPreprocessor(Preprocessor):
    # """
    # Handles all math delimiters via regex replacements:
    #   - $...$ (inline)
    #   - \( ... \) (inline)
    #   - \[ ... \] (inline or block depending on position)
    #   - $$ ... $$ (block)
    # """

    # Patterns
    RE_BLOCK_DOLLAR = re.compile(r"^\$\$\s*\n(.*?)\n\s*\$\$", re.MULTILINE | re.DOTALL)
    RE_BLOCK_BRACKET = re.compile(
        r"^\s*\\\[\s*\n(.*?)\n\s*\\\]\s*$", re.MULTILINE | re.DOTALL
    )
    # RE_INLINE_DOLLAR = re.compile(
    #     r"(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)(?<!\$)\$(?!\$)", re.DOTALL
    # )

    RE_INLINE_DOLLAR = re.compile(
        r"(?<!\\)(?<!\$)\$(?!\$)(?!\d)(.+?)(?<!\\)(?<!\$)\$(?!\$)", re.DOTALL
    )

    RE_INLINE_DOUBLEDOLLAR = re.compile(
        r"(?<!\\)(?<!\$)\$\$(?!\$)(.+?)(?<!\\)(?<!\$)\$\$(?!\$)", re.DOTALL
    )
    RE_INLINE_PAREN = re.compile(r"(?<!\\)\\\((.+?)\\\)", re.DOTALL)
    RE_INLINE_BRACKET = re.compile(r"(?<!\\)\\\[(.+?)\\\]", re.DOTALL)

    def run(self, lines):
        text = "\n".join(lines)

        original_text = text + ""

        # Block: $$ ... $$
        text = self.RE_BLOCK_DOLLAR.sub(
            lambda m: self.md.htmlStash.store(f"\\[\n{ m.group(1).strip()}\n\\]"), text
        )

        # Block: \[ ... \] (on separate lines)
        text = self.RE_BLOCK_BRACKET.sub(
            lambda m: self.md.htmlStash.store(f"\\[\n{m.group(1).strip()}\n\\]"), text
        )

        # Inline Block: $$ ... $$
        text = self.RE_INLINE_DOUBLEDOLLAR.sub(
            lambda m: self.md.htmlStash.store(f"\\[{m.group(1).strip()}\\]"), text
        )

        # Inline: $...$
        text = self.RE_INLINE_DOLLAR.sub(
            lambda m: self.md.htmlStash.store(f"\\({m.group(1).strip()}\\)"), text
        )

        # Inline: \( ... \)
        text = self.RE_INLINE_PAREN.sub(
            lambda m: self.md.htmlStash.store(f"\\(\n{m.group(1).strip()}\n\\)"), text
        )

        # Inline: \[ ... \]
        text = self.RE_INLINE_BRACKET.sub(
            lambda m: self.md.htmlStash.store(f"\\[\n{m.group(1).strip()}\n\\]"), text
        )

        if text != original_text:
            self.md.tzara_has_latex = True
        # print(original_text)
        # print(text)

        return text.split("\n")


class LaTeXExtension(Extension):
    def extendMarkdown(self, md):
        md.tzara_has_latex = False
        md.preprocessors.register(UnifiedMathPreprocessor(md), "unified-math", 9)


# from markdown.treeprocessors import Treeprocessor
# from markdown.extensions import Extension

# # Regex to detect Obsidian-style callout headers
# CALLOUT_RE = re.compile(
#     r"""
#     ^\[\!
#     (?P<type>[a-zA-Z0-9_-]+)
#     \]
#     (?P<collapse>[+-])?
#     \s*(?P<title>.*)$
#     """,
#     re.VERBOSE | re.MULTILINE | re.UNICODE,
# )
# Regex to detect Obsidian-style callout headers
CALLOUT_RE = re.compile(
    r"""
    ^\[\!
    (?P<type>[a-zA-Z0-9_-]+)
    \]
    (?P<collapse>[+-])?
    [ \t]* # <-- CHANGED: Only match spaces/tabs, NOT newlines
    (?P<title>.*)$
    """,
    re.VERBOSE | re.MULTILINE | re.UNICODE,
)


class ObsidianCalloutTreeProcessor(Treeprocessor):

    #       # abstract, summary, tldr (clipboard icon, cyan)
    #       # info (circle i icon, blue)
    #       # todo (circle checkmark, blue)
    #       # tip, hint, important (thumbs up icon, cyan)
    #       # success, check, done (checkmark icon, green)
    #       # question, help, faq (circle question mark, orange)
    #       # warning, caution, attention (triangle ! icon, orange)
    #       # failure, fail, missing (x icon, red)
    #       # danger, error (lightning bolt icon, red)
    #       # bug (bug icon, red)
    #     # example (bulleted list icon, purple)
    #     # quote (quotation mark icon, gray)

    def run(self, root):
        self._scan_for_callouts(root)

    def _scan_for_callouts(self, parent):
        """
        Scans children of the given parent. If a blockquote is found,
        it checks if it needs to be converted/split into callouts.
        """
        # Iterate over a copy of the list because we might modify the parent's children
        for i, child in enumerate(list(parent)):
            if child.tag == "blockquote":
                # Attempt to split the blockquote into a list of elements
                # (Callouts and/or regular Blockquotes)
                new_elements = self._convert_blockquote(child)

                # If conversion happened (returned a list, even of length 1)
                if new_elements is not None:
                    # 1. Handle Tail: The original blockquote might have had text after it.
                    #    Attach it to the last element of our new list.
                    if child.tail:
                        if new_elements[-1].tail:
                            new_elements[-1].tail += child.tail
                        else:
                            new_elements[-1].tail = child.tail

                    # 2. Replacement: Remove the old blockquote and insert new elements
                    parent.remove(child)
                    for j, elem in enumerate(new_elements):
                        parent.insert(i + j, elem)

                    # 3. Recursion: Scan the new elements for nested callouts
                    for elem in new_elements:
                        self._scan_for_callouts(elem)

                    # Skip normal recursion for this index since we just handled it
                    continue

            # Recurse for non-blockquote elements
            self._scan_for_callouts(child)

    def _convert_blockquote(self, blockquote):
        """
        Inspects a blockquote. If it contains callout headers, it splits the
        blockquote into a sequence of Callout Divs and (optional) regular Blockquotes.
        Returns a list of Elements, or None if no callouts were found.
        """
        # 1. Quick check: does this blockquote contain ANY callout headers?
        has_callout = False
        for child in blockquote:
            if child.tag == "p" and child.text and CALLOUT_RE.match(child.text):
                has_callout = True
                break

        if not has_callout:
            return None

        # 2. Iterate children and group them into "Chunks"
        #    A chunk is either a Callout (triggered by a header) or a generic blockquote (orphaned text)
        generated_elements = []
        current_container = None  # This will point to the 'content' div of a callout OR a generic blockquote

        for child in blockquote:
            # Check if this child is a Callout Header
            is_header = False
            match = None
            if child.tag == "p" and child.text:
                match = CALLOUT_RE.match(child.text)
                if match:
                    is_header = True

            if is_header:
                # -- START NEW CALLOUT --
                callout_type = match.group("type").lower()
                collapse = match.group("collapse")
                title_text = match.group("title")

                default_title = callout_type.capitalize()
                title_text = title_text.strip() if title_text else default_title
                title_text = title_text.strip() if title_text else default_title

                is_collapsible = collapse in ("+", "-")
                is_open = collapse == "+"

                # Create Wrapper
                wrapper = etree.Element(
                    "details" if is_collapsible else "div",
                    {
                        "class": f"callout callout-{callout_type}",
                        "data-callout": callout_type,
                        # "data-icon": icon,
                        # "data-color": color,
                    },
                )
                if is_collapsible and is_open:
                    wrapper.set("open", "open")

                # Create Title Block
                title_tag = "summary" if is_collapsible else "div"
                title_div = etree.SubElement(
                    wrapper, title_tag, {"class": "callout-title"}
                )
                # etree.SubElement(
                #     title_div, "div", {"class": "callout-icon"}
                # )  # Icon hook
                title_span = etree.SubElement(
                    title_div, "div", {"class": "callout-title-text"}
                )
                title_span.text = title_text

                # Create Content Block
                content_div = etree.SubElement(
                    wrapper, "div", {"class": "callout-content"}
                )

                # Handle regex stripping from the header paragraph
                # We do this carefully to preserve any text ON THE SAME LINE as the header
                # e.g. > [!info] Title \n This is body text on the same paragraph block
                if match.end() < len(child.text):
                    child.text = child.text[match.end() :]
                    content_div.append(child)
                elif len(child) > 0:
                    # If the P has children (like bold/italic tags) but no text left, keep it
                    child.text = None
                    content_div.append(child)
                else:
                    # Totally empty paragraph (just the header), discard it.
                    pass

                generated_elements.append(wrapper)
                current_container = content_div

            else:
                # -- NOT A HEADER --
                if current_container is None:
                    # We found normal text BEFORE any callout in this blockquote.
                    # Preserve it as a standard blockquote.
                    new_bq = etree.Element("blockquote")
                    generated_elements.append(new_bq)
                    current_container = new_bq

                # Append the child to whatever container we are currently in
                # (Either the previous Callout's content div, or the generic blockquote)
                current_container.append(child)

        return generated_elements


class ObsidianCalloutExtension(Extension):
    def extendMarkdown(self, md):
        # PRIORITY CRITICAL:
        # We must run BEFORE InlinePatterns (Priority 20) so that
        # things like `> [!info] **Bold**` are parsed as headers correctly.
        # We also run AFTER BlockParser (which builds the tree).
        # Priority 25-30 is safe, or even higher (e.g., 100) to be sure.
        md.treeprocessors.register(
            ObsidianCalloutTreeProcessor(md),
            "obsidian_callout",
            100,
        )


class AdmonitionNormalizerTreeProcessor(Treeprocessor):
    """Reshape <div class="admonition X"> (from python-markdown's built-in
    `admonition` extension, which parses MkDocs Material `!!! note "Title"`
    syntax) into the same HTML structure that ObsidianCalloutTreeProcessor
    emits for `> [!info] Title` blockquotes. Single CSS rule set then styles
    both markdown syntaxes identically.
    """

    def run(self, root):
        self._walk(root)

    def _walk(self, parent):
        for child in list(parent):
            if child.tag == "div" and "admonition" in (child.get("class") or "").split():
                self._normalize(child)
            self._walk(child)

    def _normalize(self, div):
        classes = (div.get("class") or "").split()
        # python-markdown emits e.g. class="admonition note" - pick the
        # first non-"admonition" token as the callout type.
        callout_type = next((c for c in classes if c != "admonition"), "note")

        # Snapshot existing children, separate the title paragraph from body.
        title_p = None
        body_children = []
        for c in list(div):
            if (
                title_p is None
                and c.tag == "p"
                and "admonition-title" in (c.get("class") or "").split()
            ):
                title_p = c
            else:
                body_children.append(c)

        # Clear and rebuild the div in callout shape.
        for c in list(div):
            div.remove(c)

        div.set("class", f"callout callout-{callout_type}")
        div.set("data-callout", callout_type)

        new_title = etree.SubElement(div, "div", {"class": "callout-title"})
        title_span = etree.SubElement(
            new_title, "div", {"class": "callout-title-text"}
        )
        if title_p is not None:
            title_span.text = title_p.text or callout_type.capitalize()
            for inline in list(title_p):
                title_span.append(inline)
        else:
            title_span.text = callout_type.capitalize()

        content = etree.SubElement(div, "div", {"class": "callout-content"})
        for c in body_children:
            content.append(c)


class AdmonitionNormalizerExtension(Extension):
    def extendMarkdown(self, md):
        # Lower priority than ObsidianCalloutTreeProcessor (100) so any nested
        # admonitions are still seen, but order between the two doesn't matter
        # for correctness since they target disjoint inputs (admonition divs
        # vs. blockquotes).
        md.treeprocessors.register(
            AdmonitionNormalizerTreeProcessor(md),
            "admonition_normalizer",
            90,
        )


########################
# checkbox list


class ChecklistProcessor(Treeprocessor):
    def run(self, root):
        # Iterate over all list items <li> in the document
        for li in root.iter("li"):
            # Text can be directly in the <li> or inside a <p> within the <li>
            # We check both locations for the "[ ]" or "[x]" pattern.
            target_el = li
            text = li.text

            # If the list is "loose" (contains paragraphs), the text is in the first child <p>
            if not text and len(li) > 0 and li[0].tag == "p":
                target_el = li[0]
                text = target_el.text

            if text:
                # Regex to find "[ ] " or "[x] " at start of string
                # (?i) makes it case insensitive (matches [X] or [x])
                m = re.match(r"^\[([ xX])\]\s+", text)
                if m:
                    # Determine if checked
                    is_checked = m.group(1).lower() == "x"

                    # # Checkbox html version
                    # checkbox = etree.Element("input")
                    # checkbox.set("type", "checkbox")
                    # checkbox.set("disabled", "disabled")  # Read-only by default
                    # if is_checked:
                    #     checkbox.set("checked", "checked")
                    # checkbox.tail = " " + text[m.end() :]
                    # target_el.insert(0, checkbox)
                    # target_el.text = ""
                    # li.set("class", "task-list-item")

                    # # emoji version
                    # # do we like green or purple?
                    # if is_checked:
                    #     # checkbox = "✅ "
                    #     checkbox = "☑️ "
                    #     li.set("data-checked", "checked")
                    # else:
                    #     # checkbox = "🟩 "
                    #     checkbox = "🟪 "
                    # target_el.text = checkbox + text[m.end() :]
                    # # # Optional: Add a class to the <li> for CSS styling
                    # li.set("class", "task-list-item")

                    # css version
                    span = etree.Element("span")
                    if is_checked:
                        li.set("data-checked", "checked")
                    span.text = " " + text[m.end() :]
                    target_el.insert(0, span)
                    target_el.text = ""
                    li.set("class", "task-list-item")


class ChecklistExtension(Extension):
    def extendMarkdown(self, md):
        # Register the Treeprocessor.
        # Priority 15 ensures it runs after standard list processing.
        md.treeprocessors.register(ChecklistProcessor(md), "checklist", 15)
