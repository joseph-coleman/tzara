# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

# """
# Markdown Chunker
#
# - [x] Implement line-by-line state machine with the following state tracking:
#     - [x] Current header hierarchy (stack of `(level, title)` tuples)
#     - [x] Inside code fence flag (toggle on ``` or ~~~ lines)
#     - [x] Inside LaTeX block flag (toggle on $$ lines)
#     - [x] Current chunk accumulator (list of lines)
#     - [x] Current chunk metadata (header path, wikilinks, chunk type)
# - [x] Header detection: regex `^#{1,6}\s+(.+)$`
#     - [x] On header: emit current chunk, push/pop header stack based on level, start new chunk
# - [x] Code fence detection: track ``` or ~~~ delimiters
#     - [x] Set chunk type to 'code' if chunk is entirely a code block
#     - [x] Never split inside a code fence
# - [x] LaTeX block detection: track $$ delimiters
#     - [x] Set chunk type to 'latex' if chunk is entirely a LaTeX block
#     - [x] Never split inside a LaTeX block
# - [x] Wikilink extraction: regex `\[\[([^\]|]+)(?:\|[^\]]+)?\]\]`
#     - [x] Collect all wikilinks per chunk as metadata
#     - [x] Preserve wikilinks in the chunk text (they carry semantic meaning)
# - [x] Embed detection: regex `!\[\[([^\]]+)\]\]`
#     - [x] Classify as document embed vs. asset embed (image/PDF)
#     - [x] Store asset embeds in `asset_refs`
#     - [x] Store document embeds as edges in link graph
# - [x] Tag extraction: regex `(?:^|\s)#([a-zA-Z0-9_/-]+)` (inline tags)
#     - [x] Also extract tags from YAML frontmatter `tags:` field
# - [x] Frontmatter extraction: detect `---` delimited YAML block at top of file
#     - [x] Parse YAML to extract: tags, summary, keywords, `Index` flag
#     - [x] Exclude frontmatter from chunk content
# - [x] Breadcrumb context prepending: for each chunk, prepend:
#   `"Document: {title}. Section: {header_path_joined}. "`
#     - [x] Store both raw `content` and `context_content` (prepended version)
# - [x] Chunk size management:
#     - [x] If a section under a header is very long (> max_chunk_size), split at paragraph boundaries
#     - [x] If a paragraph is very long, split on sliding sentence window for chunk < max_chunk_size
#     - [x] Never split inside code or LaTeX blocks even if oversized
# - [x] Output format: list of chunk objects, each containing:
#     - [x] `content` (raw text)
#     - [x] `context_content` (breadcrumb-prepended)
#     - [x] `header_path` (list of header titles)
#     - [x] `chunk_index` (position in document)
#     - [x] `chunk_type` ('prose', 'code', 'latex', 'header')
#     - [x] `wikilinks` (list of link targets found in this chunk)
# - [x] Ignore comments fenced by %%
# - [x] Setext-style header conversion (=== and --- underlines) to ATX style
# - [x] Nested backtick fence handling (e.g. 4-backtick fence containing 3-backtick content)
# - [x] Trailing # removal from ATX headers
# - [x] Code language extraction from fence info string
# - [x] \[ and \] bracket-style LaTeX delimiters, ignore inline LaTeX
# """


import re

from config import (
    APP_ROUTE_PREFIXES,
    ATTACHMENT_FILE_TYPES,
    IMAGE_FILE_TYPES,
    SPACE_CONVERSION_ORDER,
)


WIKILINK_RE = r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]"
EMBED_RE = r"!\[\[([^\]]+)\]\]"
TAG_RE = r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_/-]*)"
MD_HEADER = r"^#{1,6}\s+(.+)"
SETEXT_HEADER1 = r"^=+\s*$"
SETEXT_HEADER2 = r"^-+\s*$"
SENTENCE_SPLIT_RE = r'(?<=[.!?])\s+(?=[A-Z])'

# Embed targets with one of these extensions are classified as "assets" (recorded
# in asset_refs and rewritten on move) rather than document embeds. Derived from
# the served-attachment allowlist so any servable attachment type -- csv/json/xlsx
# included -- is link-tracked the same way images already are.
ASSET_EXTENSIONS = {f".{ext}" for ext in ATTACHMENT_FILE_TYPES}

# Image assets are rendered inline and aren't things the chat agent computes over,
# so they're excluded from the "attached data files" manifest (extract_data_file_refs).
# Derived from the canonical config list (dotted form) so it can't drift.
IMAGE_EXTENSIONS = {f".{ext}" for ext in IMAGE_FILE_TYPES}

# Standard markdown link target: [label](target) and image ![alt](target). Captures
# the target up to whitespace or the closing paren, tolerating an optional <...> wrap.
MD_LINK_TARGET_RE = r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)"

# Same syntax, opposite capture: the visible LABEL rather than the destination, for
# callers flattening markdown to the words a reader sees. Not image-tolerant on
# purpose -- an image's alt text is not prose (see EMBED_RE for the wiki spelling).
MD_LINK_LABEL_RE = r"\[([^\]]*)\]\([^)]*\)"

# A list marker and the space after it: bullet (-, *, +) or ordered (1.). The
# paren form `1)` is deliberately absent -- verified against the renderer
# (python-markdown + sane_lists), which emits `<p>1) Item</p>`, not a list.
# Probably should update sane_lists to fix this gap.
# Unanchored so callers compose it -- match against an already-dedented line, or
# prefix `^[ \t]*` to take the indent too.
MD_LIST_MARKER_RE = r"(?:[-*+]|\d+\.)[ \t]+"

# The task checkbox that may follow a list marker: `- [ ]`, `- [x]`, `1. [X]`.
# Separate from the marker because it is not consumed with it -- no task-list
# extension is enabled, so the renderer emits `[x]` as LITERAL text inside the
# <li>, and anything showing list content to a human should show it too.
MD_TASK_BOX_RE = r"\[[ xX]\][ \t]*"


def lawrence(body, title="", max_chunk_size=500):
    return chunk(body, title, max_chunk_size)


def _backtick_count(line):
    m = re.match(r"^`+", line)
    return len(m.group()) if m else 0


def _tilde_count(line):
    m = re.match(r"^~+", line)
    return len(m.group()) if m else 0


def _fence_info(line):
    """Check if a line starts a code fence (``` or ~~~).
    Returns (count, char) or (0, None)."""
    bc = _backtick_count(line)
    if bc >= 3:
        return bc, "`"
    tc = _tilde_count(line)
    if tc >= 3:
        return tc, "~"
    return 0, None


def _parse_frontmatter(content):
    """Delegates to the canonical WikiDoc.parse_frontmatter. Kept as a
    module-local name for chunker's callers. Lazy import avoids the
    wikidoc <-> chunker import cycle (wikidoc imports chunker at module load).
    Phase 0's parity test locked that this was byte-identical before collapsing."""
    from src.wikidoc import WikiDoc
    return WikiDoc.parse_frontmatter(content)


def _strip_frontmatter(content):
    """Delegates to the canonical WikiDoc.strip_frontmatter (lazy import: cycle)."""
    from src.wikidoc import WikiDoc
    return WikiDoc.strip_frontmatter(content)


def extract_wikilinks(text):
    """Extract wikilink targets from text, excluding embeds."""
    text_without_embeds = re.sub(EMBED_RE, "", text)
    return re.findall(WIKILINK_RE, text_without_embeds)


def md_link_page_target(target):
    """Canonicalize a markdown link target to a wikilink-style page target, or None.

    A markdown link to a page is a page link exactly like ``[[Page]]``: pages meant to
    render on an external markdown host too (where ``[[...]]`` is literal text) are
    written as ``[text](page.md)``, and the graph must count both spellings or those
    pages lose their edges. The ``.md`` is optional for authors -- ``[x](help/basics)``
    and ``[x](help/basics.md)`` mean the same file -- so the extension is stripped here
    and the canonical extensionless form is what reaches the edges table. That keeps one
    target_title per destination however the author spelled it.

    Not a page link: a target with a scheme or ``mailto:``, a bare ``#anchor``, a
    dotfile, a non-markdown extension, or a root-anchored app route ("/agents" is the
    Agent Activity page). The route test applies only to root-anchored targets, so a
    relative "agents" still resolves to a sibling document.

    Leading ``./``/``../`` hops are dropped: resolve_linkpath matches vault-globally by
    path suffix, so the prefix a filesystem-relative link needs is noise to it. Any
    ``#anchor`` is preserved for the caller to strip, matching WIKILINK_RE.
    """
    t = target.strip()
    if not t or "://" in t or t.startswith(("#", "mailto:")):
        return None
    path, sep, anchor = t.partition("#")
    path = path.strip().rstrip("/")
    absolute = path.startswith("/")

    while path.startswith(("./", "../")):
        path = path.split("/", 1)[1]
    if not path.lstrip("/"):
        return None

    base = path.rsplit("/", 1)[-1]
    if base.startswith("."):
        return None
    dot = base.rfind(".")
    if dot != -1:
        if base[dot:].lower() != ".md":
            return None
        path = path[: len(path) - (len(base) - dot)]

    if absolute and path.lstrip("/").split("/", 1)[0].lower() in APP_ROUTE_PREFIXES:
        return None
    return path + sep + anchor


def extract_page_links(text):
    """Extract every page-link target: wikilinks plus relative ``.md`` markdown links.

    This is the extractor the graph layer wants -- ``extract_wikilinks`` stays the
    narrow primitive for callers that mean the ``[[...]]`` syntax specifically.
    """
    targets = extract_wikilinks(text)
    text_without_embeds = re.sub(EMBED_RE, "", text)
    for m in re.finditer(MD_LINK_TARGET_RE, text_without_embeds):
        # MD_LINK_TARGET_RE is image-tolerant; the `![...](...)` form is an embed,
        # which extract_embeds owns, so only the plain link form is a page link.
        if m.group(0).startswith("!"):
            continue
        page = md_link_page_target(m.group(1))
        if page is not None:
            targets.append(page)
    return targets


# Wikilink separator handling. SPACE_CONVERSION_ORDER (config) lists the
# characters that are all interchangeable "spaces" in a wikilink/filename
# (e.g. "_", " ", "%20", "+"). Resolution canonicalizes both sides so
# [[Game Ideas]], [[Game_Ideas]] and a file "Game Ideas.md" all agree; the
# order itself only breaks ties when two real files collide on one key.

def wikilink_key(name):
    """Canonical match key for a wikilink target or filename stem.

    Every SPACE_CONVERSION_ORDER char becomes a single space, the result is
    lowercased, and whitespace runs are collapsed -- so 'My_File Copy',
    'My File Copy' and 'My%20File+Copy' all map to 'my file copy'.

    A leading slash is stripped first: an absolute link '[[/Folder/Page]]' is
    "from the vault root", i.e. the same target as the relative 'Folder/Page'.
    This matches how the read path resolves absolute links (resolve_page_name
    lstrips '/') so the graph index agrees with rendering. Document stem/rel/title
    keys never start with '/', so only absolute targets are affected."""
    canonical = name.lstrip("/")
    for sep in SPACE_CONVERSION_ORDER:
        if sep != " ":
            canonical = canonical.replace(sep, " ")
    return " ".join(canonical.lower().split())


def separator_rank(stem):
    """Tie-break key for filename stems that share a wikilink_key. Lower wins.

    A stem written with a single separator type ranks by that separator's
    position in SPACE_CONVERSION_ORDER (so 'Game_Ideas' -> (0, 0) beats
    'Game Ideas' -> (0, 1)). A stem mixing separator types (e.g. 'My_File Copy')
    matches no single preference, so it ranks last and only wins when alone."""
    present = [i for i, sep in enumerate(SPACE_CONVERSION_ORDER) if sep and sep in stem]
    if len(present) <= 1:
        return (0, present[0] if present else len(SPACE_CONVERSION_ORDER))
    return (1, min(present))


def normalize_separators(name, to):
    """Replace every SPACE_CONVERSION_ORDER char in `name` with `to`.

    Preserves case and collapses runs of `to`. Used when creating a new file so
    a typed 'New Idea' / 'My File Copy' becomes 'New_Idea' / 'My_File_Copy'
    (to='_') rather than a verbatim or mixed-separator name."""
    result = name
    for sep in SPACE_CONVERSION_ORDER:
        if sep != to:
            result = result.replace(sep, to)
    if to:
        while to + to in result:
            result = result.replace(to + to, to)
    return result


def _key_segments(path):
    """Canonical match segments of a vault-relative path (drops a trailing '.md').

    Each path segment is folded through wikilink_key, so 'Investment/Game Ideas.md'
    and 'investment/game_ideas' yield the same ['investment', 'game ideas'] -- the
    unit a path-suffix match compares on."""
    if path.endswith(".md"):
        path = path[:-3]
    return [wikilink_key(seg) for seg in path.split("/") if seg]


def _proximity_key(candidate, source_dir):
    """Sort key that ranks a candidate by Obsidian-style closeness to the source.

    Lower is better: folder-tree distance from source_dir first (same folder = 0, a
    direct child = 1, a sibling = 2 ...), then the shorter overall path, then
    separator_rank + lowercased path for a deterministic final tie-break."""
    src_segs = _key_segments(source_dir)
    cand_segs = _key_segments(candidate)
    cand_dir_segs = cand_segs[:-1]  # drop the basename segment
    common = 0
    for a, b in zip(src_segs, cand_dir_segs):
        if a == b:
            common += 1
        else:
            break
    distance = (len(src_segs) - common) + (len(cand_dir_segs) - common)
    rel = candidate[:-3] if candidate.endswith(".md") else candidate
    stem = rel.split("/")[-1]
    return (distance, len(cand_segs), separator_rank(stem), candidate.lower())


def resolve_linkpath(target, source_dir, candidates=None, *, by_stem=None):
    """Resolve a wikilink/embed target to a vault-relative candidate path, the way
    Obsidian does: a vault-global match by basename or path-suffix, with proximity to
    the source document breaking ties. Returns the winning path, or None.

    - ``target`` is the raw link target with any ``#anchor``/``|alias`` already
      stripped by the caller. A leading ``/`` *anchors* the match at the vault root
      (the whole path must match) instead of allowing a suffix match; otherwise any
      file whose path *ends with* the target's segments is a candidate (a bare name
      is just the single-segment case).
    - ``source_dir`` is the vault-relative folder of the source document ('' at the
      vault root); used only to break ties by proximity.
    - Supply candidate paths via ``candidates`` (any iterable of vault-relative paths)
      or, for speed, ``by_stem`` -- a ``{wikilink_key(stem): [paths]}`` map. The final
      target segment is always the basename, so by_stem lets us skip a full scan.

    Separator/case folding is per-segment via wikilink_key, so this is the single
    source of truth shared by the renderer (filesystem candidates) and the graph /
    move layers (DB doc_id candidates)."""
    t = target.strip().rstrip("/")
    if not t:
        return None
    anchored = t.startswith("/")
    tgt_segs = _key_segments(t.lstrip("/"))
    if not tgt_segs:
        return None

    if by_stem is not None:
        pool = by_stem.get(tgt_segs[-1], ())
    elif candidates is not None:
        last = tgt_segs[-1]
        pool = [c for c in candidates
                if (segs := _key_segments(c)) and segs[-1] == last]
    else:
        return None

    n = len(tgt_segs)
    matches = []
    for cand in pool:
        cand_segs = _key_segments(cand)
        if anchored:
            if cand_segs == tgt_segs:
                matches.append(cand)
        elif cand_segs[-n:] == tgt_segs:
            matches.append(cand)

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return min(matches, key=lambda c: _proximity_key(c, source_dir))


def extract_embeds(text):
    """Extract embed targets from text."""
    return re.findall(EMBED_RE, text)


def _classify_embed(target):
    """Classify an embed as 'asset' (image/PDF) or 'doc' (document)."""
    dot = target.rfind(".")
    if dot > -1:
        ext = target[dot:].lower()
        if ext in ASSET_EXTENSIONS:
            return "asset"
    return "doc"


def extract_data_file_refs(text):
    """Non-image attachment files this text links or embeds, as paths the chat
    agent can read from the run_python kernel's working directory (the page's
    folder). Returns deduped targets, order preserved.

    Two sources, treated differently because they resolve differently:
      * Standard markdown links ``[label](target)`` (the form the uploader
        inserts) are RELATIVE to the document's folder = the kernel cwd, so the
        target is kept verbatim - bare (``pitches.csv``) or foldered
        (``Other/report.pdf``, ``../data/x.csv``) alike.
      * Obsidian embeds ``![[name]]`` resolve vault-GLOBALLY by basename, so the
        bare name is only reliably in cwd when it's a sibling; a foldered embed
        target can't be trusted as a cwd-relative path, so those are dropped.
    Both are filtered to attachment extensions with images removed.
    """
    out = []
    seen = set()

    def _consider(target, allow_folder):
        # Strip an Obsidian ``|alias``/``|height`` suffix and any ``#anchor``.
        t = target.split("|", 1)[0].split("#", 1)[0].strip().lstrip("/")
        if not t:
            return
        if "/" in t and not allow_folder:
            return
        base = t.rsplit("/", 1)[-1]
        dot = base.rfind(".")
        if dot < 0:
            return
        ext = base[dot:].lower()
        if ext not in ASSET_EXTENSIONS or ext in IMAGE_EXTENSIONS:
            return
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)

    for target in extract_embeds(text):
        _consider(target, allow_folder=False)
    for target in re.findall(MD_LINK_TARGET_RE, text):
        _consider(target, allow_folder=True)
    return out


def extract_tags(text):
    """Extract inline #tags from text."""
    tags = []
    for match in re.finditer(TAG_RE, text, re.MULTILINE):
        tags.append(match.group(1))
    return list(set(tags))


def _split_sentences(text):
    """Split text into sentences using a simple heuristic.
    Returns a list; if no split points found, returns the whole text as a single-element list."""
    parts = re.split(SENTENCE_SPLIT_RE, text)
    return [p for p in parts if p]


def _sliding_window_chunks(sentences, max_size):
    """Create overlapping chunks from sentences using a sliding window with stride=1.
    Each window accumulates sentences forward from a start index until adding the next
    would exceed max_size."""
    if len(sentences) <= 1:
        return [" ".join(sentences)] if sentences else []
    
    windows = []
    last_index = len(sentences) - 1
    early_break = False 
    for start in range(len(sentences)):
        window = sentences[start]
        for end in range(start + 1, len(sentences)):
            candidate = window + " " + sentences[end]
            if len(candidate) > max_size:
                break
            elif end == last_index:
                early_break = True 

            window = candidate
            if early_break:
                break

        windows.append(window)

        if early_break:
            break

    return windows


def _split_oversized_chunk(chunk_dict, max_size):
    """Split a prose chunk at paragraph boundaries if it exceeds max_size.
    Returns a list of chunk dicts (possibly just the original if no split needed)."""
    content = chunk_dict["content"]
    if len(content) <= max_size:
        return [chunk_dict]

    paragraphs = content.split("\n\n")
    pieces = []
    current = ""

    for para in paragraphs:
        candidate = current + ("\n\n" if current else "") + para
        if len(candidate) > max_size and current:
            pieces.append(current)
            current = para
        else:
            current = candidate

    if current:
        pieces.append(current)

    # Second pass: split oversized paragraphs into sliding sentence windows
    expanded = []
    for piece in pieces:
        if len(piece) > max_size:
            sentences = _split_sentences(piece)
            if len(sentences) > 1:
                expanded.extend(_sliding_window_chunks(sentences, max_size))
            else:
                expanded.append(piece)
        else:
            expanded.append(piece)
    pieces = expanded

    splits = []
    for piece in pieces:
        new_chunk = {
            "chunk_index": 0,
            "chunk_type": chunk_dict["chunk_type"],
            "content": piece,
            "header_path": list(chunk_dict["header_path"]),
            "wikilinks": extract_page_links(piece),
            "tags": extract_tags(piece),
            "asset_refs": [],
            "doc_embeds": [],
        }
        for target in extract_embeds(piece):
            if _classify_embed(target) == "asset":
                new_chunk["asset_refs"].append(target)
            else:
                new_chunk["doc_embeds"].append(target)
        splits.append(new_chunk)

    return splits


def _split_oversized_by_lines(chunk_dict, max_size):
    """Split a NON-prose chunk (code, latex, header) at line boundaries when it
    exceeds max_size.

    Paragraph/sentence splitting (``_split_oversized_chunk``) is wrong for code -- it
    would shred syntax -- so here we accumulate whole lines up to the budget, and
    hard-slice any single line that alone exceeds it (e.g. a minified JS line or a very
    wide table row). This is the fix for the root cause of embedding "input too large"
    errors: previously only ``prose`` chunks were size-bounded, so a big fenced code
    block sailed through at thousands of tokens. Returns a list of chunk dicts.
    """
    content = chunk_dict["content"]
    if len(content) <= max_size:
        return [chunk_dict]

    pieces = []
    current = ""
    for line in content.split("\n"):
        if len(line) > max_size:
            # A single line over budget: flush what we have, then hard-slice the line.
            if current:
                pieces.append(current)
                current = ""
            for i in range(0, len(line), max_size):
                pieces.append(line[i:i + max_size])
            continue
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > max_size and current:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)

    # Mirror the post-processing extraction rules: code chunks carry no wikilinks/
    # embeds/tags; other non-prose types (latex, header) get links/embeds but not tags.
    is_code = chunk_dict["chunk_type"] == "code"
    splits = []
    for piece in pieces:
        new_chunk = {
            "chunk_index": 0,
            "chunk_type": chunk_dict["chunk_type"],
            "content": piece,
            "header_path": list(chunk_dict["header_path"]),
            "wikilinks": [] if is_code else extract_page_links(piece),
            "tags": [],
            "asset_refs": [],
            "doc_embeds": [],
        }
        if not is_code:
            for target in extract_embeds(piece):
                if _classify_embed(target) == "asset":
                    new_chunk["asset_refs"].append(target)
                else:
                    new_chunk["doc_embeds"].append(target)
        splits.append(new_chunk)
    return splits


def chunk(body, title="", max_chunk_size=2000):
    """Return a """

    # --- Frontmatter extraction ---
    frontmatter = _parse_frontmatter(body)
    body = _strip_frontmatter(body)

    fm_tags = []
    if "tags" in frontmatter:
        # Plain split of ALL frontmatter tags for indexing. NOT
        # WikiDoc.extract_manual_tags (that returns only !-prefixed manual tags)
        # nor parse_ollama_tags (LLM-output parser) - different semantics.
        fm_tags = [t.strip() for t in frontmatter["tags"].split(",") if t.strip()]

    if not title and "title" in frontmatter:
        title = frontmatter["title"]

    # --- Preparse: drop `%%` block comments ---
    # Shared with the renderer (ObsidianCommentExtension) so the index and the
    # page cannot disagree about what counts as a comment. It runs BEFORE the
    # setext pass below so an `===` underline inside a comment is not read as
    # document structure. Lazy import breaks the md_sections <-> chunker cycle
    # (md_sections imports _fence_info from here), same as _parse_frontmatter.
    from src.md_sections import strip_comment_blocks
    body = strip_comment_blocks(body)

    lines = body.split("\n")

    NON_WHITESPACE = r"\S+"
    WHITESPACE = r"^\s+$"

    # --- Preparse: convert setext headers to ATX style ---
    # Setext headers require a lookback (text on previous line, blank before that).
    for n in range(len(lines)):
        line = lines[n]
        if re.match(SETEXT_HEADER1, line) or re.match(SETEXT_HEADER2, line):
            if n >= 2:
                if re.match(NON_WHITESPACE, lines[n - 1]) and (
                    re.match(WHITESPACE, lines[n - 2]) or len(lines[n - 2]) == 0
                ):
                    current_header_level = 1 if line[0] == "=" else 2
                    header_text = lines[n - 1]
                    lines[n] = f"{'#' * current_header_level} {header_text}"
                    lines[n - 1] = ""
                    lines[n - 2] = ""

    # --- State machine ---
    in_code_block = False
    in_latex_block = False
    fence_count = 0
    fence_char = None
    new_block_needed = False

    header_stack = []  # list of (level, title) tuples
    chunk_count = 0
    document_chunks = [
        {
            "chunk_index": 0,
            "content": "",
            "chunk_type": "prose",
            "header_path": [],
            "wikilinks": [],
            "tags": [],
            "asset_refs": [],
            "doc_embeds": [],
        }
    ]

    def current_header_path():
        return [h[1] for h in header_stack]

    def make_chunk(chunk_type, **extra):
        nonlocal chunk_count
        chunk_count += 1
        c = {
            "chunk_index": chunk_count,
            "chunk_type": chunk_type,
            "content": "",
            "header_path": current_header_path(),
            "wikilinks": [],
            "tags": [],
            "asset_refs": [],
            "doc_embeds": [],
        }
        c.update(extra)
        document_chunks.append(c)

    for n in range(len(lines)):
        line = lines[n]

        # --- Code fence detection (backticks and tildes) ---
        fc, fchar = _fence_info(line)
        is_fence_line = fc >= 3 and not in_latex_block

        if is_fence_line:
            if not in_code_block:
                remainder = line[fc:]
                # Backtick fences: check for inline/self-closing (same count appears again)
                if fchar == "`" and ("`" * fc) in remainder:
                    pass  # inline backtick span, treat as regular text
                else:
                    fence_count = fc
                    fence_char = fchar
                    code_language = remainder.strip()
                    in_code_block = True
                    make_chunk("code", language=code_language)
                    new_block_needed = False
                    continue
            elif fchar == fence_char and fc == fence_count:
                # Closing fence: same char, same count
                in_code_block = False
                fence_count = 0
                fence_char = None
                new_block_needed = True
                continue
            # else: mismatched fence inside a code block, falls through to content

        # `%%` block comments are already gone - stripped in the preparse above
        # rather than toggled here, because a single-pass toggle cannot honor the
        # "unterminated opener is not a delimiter" rule and swallows to EOF.

        # --- LaTeX block detection: $$ ---
        if re.match(r"^\$\$\s*$", line) and not in_code_block:
            if not in_latex_block:
                in_latex_block = True
                make_chunk("latex")
                new_block_needed = False
            else:
                in_latex_block = False
                new_block_needed = True
            continue

        # --- LaTeX block detection: \[ and \] ---
        if line.rstrip() == "\\[" and not in_code_block and not in_latex_block:
            in_latex_block = True
            make_chunk("latex")
            new_block_needed = False
            continue

        if line.rstrip() == "\\]" and not in_code_block and in_latex_block:
            in_latex_block = False
            new_block_needed = True
            continue

        # --- Inside code or latex block: accumulate content ---
        if in_code_block or in_latex_block:
            document_chunks[chunk_count]["content"] += line + "\n"
            continue

        # --- Header detection ---
        if re.match(MD_HEADER, line):
            split_string = line.split(None, maxsplit=1)
            if len(split_string) != 2:
                continue
            hashes, header_text = split_string
            current_header_level = min(len(hashes), 6)
            header_text = header_text.strip()

            # Strip trailing # marks (e.g. "## Heading ##")
            m = re.search(r"#+$", header_text)
            if m:
                header_text = header_text[: m.start()].strip()

            # Update header hierarchy stack
            while header_stack and header_stack[-1][0] >= current_header_level:
                header_stack.pop()
            header_stack.append((current_header_level, header_text))

            make_chunk("header", header_level=current_header_level)
            document_chunks[chunk_count]["content"] = header_text
            new_block_needed = True
            continue

        # --- Regular text ---
        if new_block_needed:
            make_chunk("prose")
            new_block_needed = False

        document_chunks[chunk_count]["content"] += line + "\n"

    # --- Post-processing ---

    # Extract wikilinks, embeds, and tags from each chunk
    for c in document_chunks:
        if c["chunk_type"] != "code":
            content = c["content"]
            c["wikilinks"] = extract_page_links(content)
            for target in extract_embeds(content):
                if _classify_embed(target) == "asset":
                    c["asset_refs"].append(target)
                else:
                    c["doc_embeds"].append(target)
            if c["chunk_type"] == "prose":
                c["tags"] = extract_tags(content)

    # Bound EVERY chunk_type to max_chunk_size so none reaches the embedder oversized.
    # Prose splits at paragraph -> sentence boundaries; non-prose (code/latex/header)
    # splits at line boundaries (paragraph/sentence splitting would shred code).
    final_chunks = []
    for c in document_chunks:
        if len(c["content"]) <= max_chunk_size:
            final_chunks.append(c)
        elif c["chunk_type"] == "prose":
            final_chunks.extend(_split_oversized_chunk(c, max_chunk_size))
        else:
            final_chunks.extend(_split_oversized_by_lines(c, max_chunk_size))

    # Re-index and add breadcrumb context
    for i, c in enumerate(final_chunks):
        c["chunk_index"] = i

        header_path_str = " > ".join(c["header_path"]) if c["header_path"] else ""
        breadcrumb_parts = []
        if title:
            breadcrumb_parts.append(f"Document: {title}")
        if header_path_str:
            breadcrumb_parts.append(f"Section: {header_path_str}")

        prefix = ". ".join(breadcrumb_parts) + ". " if breadcrumb_parts else ""
        c["context_content"] = prefix + c["content"]

    return {
        "frontmatter": frontmatter,
        "frontmatter_tags": fm_tags,
        "title": title,
        "chunks": final_chunks,
    }

