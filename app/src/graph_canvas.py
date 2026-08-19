# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Auto-generated wiki connection graph -- standalone, testable functions.

The wiki's link topology already lives in Postgres (the `edges` table, populated
by the RAG indexer on every save). This module turns that topology into an
Obsidian-style canvas document the TzaraCanvas renderer can draw:

    Postgres edges/documents  ->  fetch_graph()  ->  layout_graph() (x,y via
    networkx spring layout)  ->  to_canvas_json()  ->  {nodes, edges}

Three functions, kept separate so each is unit-testable in isolation:
  - fetch_graph(root_doc_id, depth)   -- query the link graph (global or local)
  - layout_graph(nodes, edges)        -- assign pixel positions per doc_id
  - to_canvas_json(nodes, edges, pos) -- emit TzaraCanvas {nodes, edges} JSON
"""

import hashlib
import logging
import math
import threading
from urllib.parse import quote

import networkx as nx
import psycopg2
import psycopg2.extras

from src.chunker import wikilink_key
from config import (
    DEFAULT_VAULT,
)

logger = logging.getLogger("graph_canvas")

# The vault whose graph is currently being built, so the deep node-building helpers
# can emit vault-explicit /wiki/{vault}/ hrefs without threading vault through every
# layout function. Thread-local because build_canvas runs in a per-request worker
# thread (asyncio.to_thread).
_href_ctx = threading.local()

# Page-node colors encode index/existence state (presets round-trip to the
# .canvas schema; tzara's theme maps them to its hues):
#   ghost     (doc_exists=FALSE)  -> a page that USED to exist and was deleted,
#                                    still kept because something links to it
#   missing   (_is_missing)       -> a page that has NEVER existed but a document
#                                    links to it -- a "yet to be created" target,
#                                    synthesized from unresolved edges (no row)
#   link-only (rag_indexed=FALSE) -> real page present in the link graph but not
#                                    embedded for RAG (e.g. Index:False hubs)
#   indexed   -> default paper (no color key)
# Embed edges differ from wikilink edges by color too.
_COLOR_GHOST = "1"
_COLOR_LINK_ONLY = "2"
_COLOR_EMBED = "4"
# Missing/uncreated targets get teal (base-180) so they read as invitingly
# distinct from the red deletion-ghost preset rather than as an error state.
_COLOR_MISSING = "5"
# Tag nodes (opt-in #tag overlay) get their own preset so the legend can name
# them and they read as distinct from page-state colors.
_COLOR_TAG = "6"

# Node geometry: BOTH dimensions grow with link degree, so hubs read as bigger
# cards instead of just wider pills. All values in canvas world pixels.
_NODE_W_MIN = 140
_NODE_W_MAX = 280
_NODE_W_PER_DEGREE = 14
_NODE_H_MIN = 48
_NODE_H_MAX = 88
_NODE_H_PER_DEGREE = 4
_NODE_H = 56  # fixed strip height for folder-note headers / structural math

# Folder-box layout tuning (canvas world pixels).
_FOLDER_PAD = 20.0         # inner padding around a folder's content
_FOLDER_LABEL_PAD = 48.0   # extra top padding so content clears the folder label
_FOLDER_GAP = 40.0         # gap between sibling items inside a folder
_FOLDER_ROW_WIDTH = 2400.0  # wrap a folder's children to a new row past this width


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_pg_connection():
    from config import get_pg_connection
    return get_pg_connection()


def _node_id(doc_id: str) -> str:
    """Stable, unique canvas node id derived from a doc_id."""
    return "n" + hashlib.md5(doc_id.encode("utf-8")).hexdigest()[:12]


def _doc_href(doc_id: str) -> str:
    """Map a doc_id to its viewable wiki URL (drop the .md extension).

    Percent-encode the path so spaces and other unsafe chars don't break the
    markdown link the node body uses; the /wiki route unquote()s it on the way
    back in (WikiDoc.parse_url_path). Slashes are preserved as separators.
    """
    path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
    vault = getattr(_href_ctx, "vault", DEFAULT_VAULT)
    return f"/wiki/{vault}/" + quote(path, safe="/")


def _md_link_escape(text: str) -> str:
    """Escape characters that would break a markdown link label."""
    return text.replace("\\", "\\\\").replace("]", "\\]").replace("[", "\\[")


def _bfs_neighborhood(root_doc_id, edge_list, depth):
    """Undirected BFS from root_doc_id out `depth` hops over edge_list.

    Returns the set of reachable doc_ids (including the root). Edges are treated
    as undirected so backlinks count as connections, matching how Obsidian's
    local graph behaves.
    """
    adj: dict[str, set[str]] = {}
    for src, tgt, _ in edge_list:
        adj.setdefault(src, set()).add(tgt)
        adj.setdefault(tgt, set()).add(src)

    seen = {root_doc_id}
    frontier = {root_doc_id}
    for _ in range(max(0, depth)):
        nxt: set[str] = set()
        for node in frontier:
            for neighbor in adj.get(node, ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    nxt.add(neighbor)
        if not nxt:
            break
        frontier = nxt
    return seen


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def fetch_graph(root_doc_id: str | None = None, depth: int = 1, include_isolated: bool = False,
                vault_id: str | None = None):
    """Query the wiki link graph from Postgres.

    Args:
        root_doc_id: if given, return only the local neighborhood around this
            doc (BFS out `depth` hops, both directions). If None, the global graph.
        depth: BFS radius for the local graph.
        include_isolated: for the global graph, whether to include nodes with no
            resolved links (orphans). Defaults to False -- the global view is
            about *connections*, so orphans are hidden (Obsidian's default too).

    Returns:
        (nodes, edges) where
          nodes = [{doc_id, title, doc_exists, rag_indexed, degree}, ...]
          edges = [{source, target, edge_type}, ...]   (source/target are doc_ids)

    Only resolved edges (both endpoints are real document rows) are used, so
    every edge endpoint is guaranteed to be present as a node. On any DB error
    an empty ([], []) is returned so callers can render an empty canvas.
    """
    try:
        conn = _get_pg_connection()
    except Exception as e:  # pragma: no cover - environment dependent
        logger.warning("graph_canvas: could not connect to Postgres: %s", e)
        return [], []

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT source_doc_id, target_doc_id, edge_type
            FROM edges
            WHERE resolved = TRUE AND target_doc_id IS NOT NULL
              AND (%(vault_id)s IS NULL OR vault_id = %(vault_id)s)
            """,
            {"vault_id": vault_id},
        )
        raw_edges = [
            (r["source_doc_id"], r["target_doc_id"], r["edge_type"])
            for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT doc_id, title, doc_exists, rag_indexed FROM documents
               WHERE (%(vault_id)s IS NULL OR vault_id = %(vault_id)s)""",
            {"vault_id": vault_id},
        )
        docs = {
            r["doc_id"]: {
                "doc_id": r["doc_id"],
                "title": r["title"],
                "doc_exists": bool(r["doc_exists"]),
                "rag_indexed": bool(r["rag_indexed"]),
            }
            for r in cur.fetchall()
        }
    finally:
        conn.close()

    # Keep only edges whose endpoints both exist as documents (and no self-loops).
    edge_list = [
        (s, t, et)
        for (s, t, et) in raw_edges
        if s in docs and t in docs and s != t
    ]

    if root_doc_id is not None:
        keep = _bfs_neighborhood(root_doc_id, edge_list, depth)
        edge_list = [(s, t, et) for (s, t, et) in edge_list if s in keep and t in keep]
        node_ids = (keep & set(docs)) | ({root_doc_id} if root_doc_id in docs else set())
    else:
        node_ids = set(docs)

    # Degree (undirected) over the surviving edges -- drives node sizing.
    degree: dict[str, int] = {nid: 0 for nid in node_ids}
    for s, t, _ in edge_list:
        degree[s] = degree.get(s, 0) + 1
        degree[t] = degree.get(t, 0) + 1

    if root_doc_id is None and not include_isolated:
        node_ids = {nid for nid in node_ids if degree.get(nid, 0) > 0}

    nodes = [{**docs[nid], "degree": degree.get(nid, 0)} for nid in node_ids]
    edges = [{"source": s, "target": t, "edge_type": et} for (s, t, et) in edge_list]
    return nodes, edges


# Layout tuning (all in canvas world pixels).
_EDGE_LEN = 240.0       # target spring edge length
_PACK_MARGIN = 300.0    # gap between packed components (> node width so they never touch)
_PACK_ROW_WIDTH = 3600.0  # wrap components to a new shelf row past this width

# Radial (local-view) tuning.
_RING_GAP = 320.0       # base radius added per BFS hop from the focused page
_RING_MIN_ARC = 340.0   # min arc length reserved per node so crowded rings expand


def _resolve_overlaps(pos, sizes, margin=48.0, iterations=120):
    """Push apart overlapping node rectangles in place (centers in `pos`).

    spring_layout treats nodes as dimensionless points, so finite-size cards
    placed near each other visually collide. This separating-axis pass shifts
    each overlapping pair halfway apart on the axis of *least* penetration --
    minimal disturbance to the layout's shape while guaranteeing separation.
    """
    ids = list(pos.keys())
    n = len(ids)
    for _ in range(iterations):
        moved = False
        for i in range(n):
            a = ids[i]
            ax, ay = pos[a]
            aw, ah = sizes.get(a, (_NODE_W_MIN, _NODE_H))
            for j in range(i + 1, n):
                b = ids[j]
                bx, by = pos[b]
                bw, bh = sizes.get(b, (_NODE_W_MIN, _NODE_H))
                dx, dy = bx - ax, by - ay
                ox = (aw + bw) / 2.0 + margin - abs(dx)
                oy = (ah + bh) / 2.0 + margin - abs(dy)
                if ox > 0.0 and oy > 0.0:
                    if ox <= oy:
                        sh = ox / 2.0 if dx >= 0 else -ox / 2.0
                        ax, bx = ax - sh, bx + sh
                    else:
                        sh = oy / 2.0 if dy >= 0 else -oy / 2.0
                        ay, by = ay - sh, by + sh
                    pos[a] = (ax, ay)
                    pos[b] = (bx, by)
                    moved = True
        if not moved:
            break
    return pos


def layout_graph(nodes, edges) -> dict[str, tuple[float, float]]:
    """Assign a (x, y) pixel position to each node's doc_id.

    Each connected component is laid out independently with networkx
    spring_layout (Fruchterman-Reingold), overlaps are removed (spring ignores
    node size), then components are *shelf-packed* by their size-aware bounding
    boxes (left-to-right, wrapping to new rows). This keeps the canvas compact
    regardless of how lopsided the component sizes are -- a single large hub no
    longer forces every 2-node pair into a hub-sized grid cell, the failure mode
    of naive uniform tiling.
    """
    if not nodes:
        return {}

    sizes = {nd["doc_id"]: _page_size(nd) for nd in nodes}
    G = nx.Graph()
    G.add_nodes_from(n["doc_id"] for n in nodes)
    G.add_edges_from((e["source"], e["target"]) for e in edges)

    components = sorted(nx.connected_components(G), key=len, reverse=True)

    # Lay out each component locally and normalize so its (size-aware) bounding
    # box starts at the origin; record (local_positions, width, height) for packing.
    boxes: list[tuple[dict[str, tuple[float, float]], float, float]] = []
    for comp in components:
        m = len(comp)
        if m == 1:
            local = {next(iter(comp)): (0.0, 0.0)}
        elif m == 2:
            # A clean horizontal pair reads better than spring_layout's diagonal.
            a, b = sorted(comp)
            local = {a: (0.0, 0.0), b: (_EDGE_LEN + _NODE_W_MAX, 0.0)}
        else:
            sub = G.subgraph(comp)
            raw = nx.spring_layout(sub, seed=42, k=1.4 / math.sqrt(m), iterations=150)
            # Scale normalized [-1,1] out to roughly edge-length + node-width so
            # there's room before overlap resolution nudges the rest apart.
            s = (_EDGE_LEN + _NODE_W_MIN) * math.sqrt(m) / 2.0
            local = {node: (float(x) * s, float(y) * s) for node, (x, y) in raw.items()}
            _resolve_overlaps(local, sizes)

        # Size-aware bbox: include each node's half-extent so shelf-packed
        # components never touch and intra-component cards stay clear.
        lefts = [x - sizes.get(n, (_NODE_W_MIN, _NODE_H))[0] / 2.0 for n, (x, _y) in local.items()]
        rights = [x + sizes.get(n, (_NODE_W_MIN, _NODE_H))[0] / 2.0 for n, (x, _y) in local.items()]
        tops = [y - sizes.get(n, (_NODE_W_MIN, _NODE_H))[1] / 2.0 for n, (_x, y) in local.items()]
        bottoms = [y + sizes.get(n, (_NODE_W_MIN, _NODE_H))[1] / 2.0 for n, (_x, y) in local.items()]
        min_x, min_y = min(lefts), min(tops)
        w = (max(rights) - min_x) or float(_NODE_W_MAX)
        h = (max(bottoms) - min_y) or float(_NODE_H)
        local = {n: (x - min_x, y - min_y) for n, (x, y) in local.items()}
        boxes.append((local, w, h))

    # Shelf-pack: place boxes along a row until it would exceed _PACK_ROW_WIDTH,
    # then wrap down by the tallest box in the row so far.
    positions: dict[str, tuple[float, float]] = {}
    cx = cy = 0.0
    row_h = 0.0
    for local, w, h in boxes:
        if cx > 0.0 and cx + w > _PACK_ROW_WIDTH:
            cx = 0.0
            cy += row_h + _PACK_MARGIN
            row_h = 0.0
        for doc_id, (x, y) in local.items():
            positions[doc_id] = (cx + x, cy + y)
        cx += w + _PACK_MARGIN
        row_h = max(row_h, h)
    return positions


def layout_radial(nodes, edges, root_doc_id) -> dict[str, tuple[float, float]]:
    """Concentric-ring layout for the local (neighborhood) view.

    The focused page sits at the origin; its direct links land on the first
    ring, their links on the next, and so on (BFS hop count = ring index). This
    reads as "this page and what surrounds it", which spring_layout + shelf-pack
    never conveyed. Returns node CENTERS keyed by doc_id.

    Within a ring, nodes are ordered by their parent's angle so siblings of the
    same parent stay adjacent (fewer edge crossings). A ring's radius grows if it
    would otherwise pack nodes closer than `_RING_MIN_ARC`. Falls back to
    `layout_graph` if the root isn't present (shouldn't happen for a BFS graph).
    """
    node_ids = {n["doc_id"] for n in nodes}
    if not node_ids:
        return {}
    if root_doc_id not in node_ids:
        return layout_graph(nodes, edges)

    adj: dict[str, set[str]] = {}
    for e in edges:
        s, t = e["source"], e["target"]
        if s in node_ids and t in node_ids:
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)

    # BFS from the root, recording hop depth and the parent each node came from.
    depth: dict[str, int] = {root_doc_id: 0}
    parent: dict[str, str | None] = {root_doc_id: None}
    frontier = [root_doc_id]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for nb in sorted(adj.get(node, ())):
                if nb not in depth:
                    depth[nb] = depth[node] + 1
                    parent[nb] = node
                    nxt.append(nb)
        frontier = nxt

    # Anything unreachable (disconnected within the kept set) goes on an outer ring.
    if depth:
        outer = max(depth.values()) + 1
        for nid in node_ids:
            depth.setdefault(nid, outer)
            parent.setdefault(nid, None)

    rings: dict[int, list[str]] = {}
    for nid in node_ids:
        rings.setdefault(depth[nid], []).append(nid)

    positions: dict[str, tuple[float, float]] = {root_doc_id: (0.0, 0.0)}
    angle: dict[str, float] = {root_doc_id: 0.0}
    prev_r = 0.0
    for d in sorted(k for k in rings if k >= 1):
        ring = sorted(rings[d], key=lambda n: (angle.get(parent.get(n) or "", 0.0), n))
        count = len(ring)
        # Grow the radius if the default ring would crowd nodes closer than the
        # minimum arc length (circumference / count >= _RING_MIN_ARC), but keep
        # radii strictly increasing so a sparse outer ring never lands inside a
        # crowded inner one.
        r = max(d * _RING_GAP, count * _RING_MIN_ARC / (2.0 * math.pi), prev_r + _RING_GAP)
        prev_r = r
        for i, nid in enumerate(ring):
            a = (2.0 * math.pi * i / count) if count else 0.0
            angle[nid] = a
            positions[nid] = (r * math.cos(a), r * math.sin(a))
    return positions


def _edge_sides(sx, sy, tx, ty):
    """Pick from/to attachment sides so arrows point sensibly between centers."""
    dx, dy = tx - sx, ty - sy
    if abs(dx) >= abs(dy):
        return ("right", "left") if dx >= 0 else ("left", "right")
    return ("bottom", "top") if dy >= 0 else ("top", "bottom")


def _page_size(nd) -> tuple[int, int]:
    """Pixel (width, height) of a page node -- BOTH dims grow with link degree.

    A folder-note pinned as a folder header carries an explicit `_render_width`
    (set in `_pack_folder`) so its layout size matches the full-width box header;
    such headers keep the fixed strip height. The focused page in a local view
    (`_is_root`) gets a size bump so the eye lands on it first.
    """
    override = nd.get("_render_width")
    if override is not None:
        return int(override), _NODE_H
    deg = nd.get("degree", 0)
    w = min(_NODE_W_MAX, _NODE_W_MIN + deg * _NODE_W_PER_DEGREE)
    h = min(_NODE_H_MAX, _NODE_H_MIN + deg * _NODE_H_PER_DEGREE)
    if nd.get("_is_root"):
        w = max(w, _NODE_W_MIN + 80)
        h = _NODE_H_MAX
    return int(w), int(h)


def _node_color(nd) -> str | None:
    """Color preset encoding a page's state, or None for a default-paper node."""
    if nd.get("_is_tag"):
        return _COLOR_TAG
    if nd.get("_is_missing"):
        return _COLOR_MISSING
    if not nd.get("doc_exists", True):
        return _COLOR_GHOST
    if not nd.get("rag_indexed", True):
        return _COLOR_LINK_ONLY
    return None


def _page_node(nd, cx: float, cy: float) -> dict:
    """A TzaraCanvas `text` node centered at (cx, cy).

    Page nodes carry a markdown link `[Title](/wiki/...)` so the read-only canvas
    shows the title and clicking navigates. Tag nodes (`_is_tag`) carry plain
    `#tag` text -- they're an overlay, not a destination.
    """
    doc_id = nd["doc_id"]
    w, h = _page_size(nd)
    if nd.get("_is_tag"):
        # Backslash-escape the leading '#': unescaped it's Markdown ATX-heading
        # syntax, so the renderer blew the tag label up into a giant <h1>.
        text = "\\#" + _md_link_escape(nd.get("tag") or "")
    else:
        # Missing nodes have a synthetic doc_id, so link to their real target path
        # (opens the editor in create mode); real pages link to their own doc_id.
        href = _doc_href(nd["missing_path"] if nd.get("_is_missing") else doc_id)
        text = f"[{_md_link_escape(nd.get('title') or doc_id)}]({href})"
    node = {
        "id": _node_id(doc_id),
        "type": "text",
        "x": int(round(cx - w / 2)),
        "y": int(round(cy - h / 2)),
        "width": w,
        "height": h,
        "text": text,
    }
    color = _node_color(nd)
    if color:
        node["color"] = color
    return node


def _edges_json(edges, id_map: dict, centers: dict) -> list:
    """Serialize edges, choosing attachment sides from node centers. Edges whose
    endpoints aren't in id_map (filtered-out nodes) are skipped."""
    out = []
    for i, e in enumerate(edges):
        is_embed = e.get("edge_type") == "embed"
        # Edges are stored source=embedder/linker, target=embedded/linked. For an
        # embed the target's content is pulled UP INTO the source (`Test` contains
        # ![[FunStuff]] -> FunStuff lives inside Test), so we draw the arrow the
        # other way -- from the embedded page into its container -- and the
        # arrowhead lands on the container. Wikilinks/tags keep source -> target.
        from_key, to_key = (e["target"], e["source"]) if is_embed else (e["source"], e["target"])
        s_id = id_map.get(from_key)
        t_id = id_map.get(to_key)
        if not s_id or not t_id:
            continue
        sx, sy = centers.get(from_key, (0.0, 0.0))
        tx, ty = centers.get(to_key, (0.0, 0.0))
        from_side, to_side = _edge_sides(sx, sy, tx, ty)
        edge = {
            "id": f"e{i}",
            "fromNode": s_id,
            "toNode": t_id,
            "fromSide": from_side,
            "toSide": to_side,
        }
        # Wikilink/tag edges are quiet connectors (no arrowhead). Embeds keep a
        # colored arrow so the directional "contained-in" relationship stays legible.
        if is_embed:
            edge["color"] = _COLOR_EMBED
            edge["toEnd"] = "arrow"
        out.append(edge)
    return out


def to_canvas_json(nodes, edges, positions) -> dict:
    """Build a TzaraCanvas document ({nodes, edges}) from graph + positions.

    Used by the local (neighborhood) view; `positions` are node CENTERS.
    """
    id_map: dict[str, str] = {}
    centers: dict[str, tuple[float, float]] = {}
    out_nodes = []
    for nd in nodes:
        doc_id = nd["doc_id"]
        cx, cy = positions.get(doc_id, (0.0, 0.0))
        out_nodes.append(_page_node(nd, cx, cy))
        id_map[doc_id] = _node_id(doc_id)
        centers[doc_id] = (cx, cy)
    return {"nodes": out_nodes, "edges": _edges_json(edges, id_map, centers)}


# ---------------------------------------------------------------------------
# Folder-grouped layout (global view): pages nest in labeled folder boxes,
# wikilinks drawn as edges that may cross boxes. TzaraCanvas group membership is
# purely spatial, so a folder box is just a sized/positioned rectangle drawn
# behind its member nodes -- no membership field to set.
# ---------------------------------------------------------------------------

def _folder_tree(nodes) -> dict:
    """Nest nodes into a folder tree keyed by their doc_id path components."""
    root = {"name": "", "path": "", "subfolders": {}, "pages": []}
    for nd in nodes:
        parts = nd["doc_id"].split("/")
        folders = parts[:-1]
        cur = root
        for f in folders:
            child = cur["subfolders"].get(f)
            if child is None:
                child_path = (cur["path"] + "/" + f) if cur["path"] else f
                child = {"name": f, "path": child_path, "subfolders": {}, "pages": []}
                cur["subfolders"][f] = child
            cur = child
        cur["pages"].append(nd)
    return root


def _page_stem(doc_id: str) -> str:
    """Last path component of a doc_id, minus a trailing `.md` (e.g. 'Programming')."""
    name = doc_id.split("/")[-1]
    return name[:-3] if name.endswith(".md") else name


def _attach_folder_notes(folder) -> None:
    """Recursively pull each folder's 'folder note' page out of the page list and
    pin it on the owning subfolder as `index_page`.

    A folder note is the home/index page that shares a folder's name (the Obsidian
    convention). Two layouts are recognized, matched by `wikilink_key` so they
    survive separator/case differences:
      - sibling: `Programming.md` sitting NEXT TO the `Programming/` folder
      - inside:  `Programming/Programming.md` sitting INSIDE it
    Sibling wins if both exist; on ties the first match wins (deterministic given
    the tree's insertion order)."""
    sub_by_key = {wikilink_key(name): name for name in folder["subfolders"]}

    # Sibling convention: a page here whose stem names one of our subfolders.
    remaining = []
    for nd in folder["pages"]:
        sub_name = sub_by_key.get(wikilink_key(_page_stem(nd["doc_id"])))
        sub = folder["subfolders"].get(sub_name) if sub_name else None
        if sub is not None and sub.get("index_page") is None:
            sub["index_page"] = nd
        else:
            remaining.append(nd)
    folder["pages"] = remaining

    # Inside convention: a subfolder containing a page named after itself.
    for sub in folder["subfolders"].values():
        if sub.get("index_page") is None:
            for nd in sub["pages"]:
                if wikilink_key(_page_stem(nd["doc_id"])) == wikilink_key(sub["name"]):
                    sub["index_page"] = nd
                    sub["pages"].remove(nd)
                    break
        _attach_folder_notes(sub)


def _shelf_pack(items, row_width: float, gap: float):
    """Shelf-pack (key, w, h) items left-to-right, wrapping past row_width.

    Returns (placed, total_w, total_h) where placed maps key -> (x, y) top-left.
    """
    placed: dict = {}
    cx = cy = 0.0
    row_h = 0.0
    total_w = 0.0
    for key, w, h in items:
        if cx > 0.0 and cx + w > row_width:
            cx = 0.0
            cy += row_h + gap
            row_h = 0.0
        placed[key] = (cx, cy)
        cx += w + gap
        row_h = max(row_h, h)
        total_w = max(total_w, cx - gap)
    return placed, total_w, cy + row_h


def _pack_folder(folder, depth: int):
    """Recursively lay out a folder's pages + subfolder boxes.

    Returns (positions, boxes, outer_w, outer_h):
      positions: doc_id -> (center_x, center_y), relative to this folder's OUTER
                 top-left (0, 0).
      boxes:     folder group rects {path, name, depth, x, y, w, h}, including this
                 folder's own box (except the unnamed root) and all nested boxes.
    """
    is_root = depth == 0
    pad = 0.0 if is_root else _FOLDER_PAD
    label_pad = 0.0 if is_root else _FOLDER_LABEL_PAD

    page_nodes = {nd["doc_id"]: nd for nd in folder["pages"]}
    sub_results: dict = {}
    items: list = []
    for name in sorted(folder["subfolders"]):
        spos, sboxes, sw, sh = _pack_folder(folder["subfolders"][name], depth + 1)
        sub_results[name] = (spos, sboxes, sw, sh)
        items.append((("sub", name), sw, sh))
    for nd in sorted(folder["pages"], key=lambda n: (n.get("title") or n["doc_id"]).lower()):
        pw, ph = _page_size(nd)
        items.append((("page", nd["doc_id"]), pw, ph))

    placed, interior_w, interior_h = _shelf_pack(items, _FOLDER_ROW_WIDTH, _FOLDER_GAP)
    if not items:
        interior_w, interior_h = float(_NODE_W_MIN), float(_NODE_H)

    # Folder note (this folder's index page): pin it as a full-width header above
    # the packed children. `_attach_folder_notes` already removed it from `pages`,
    # so it never entered the shelf-pack. The header width tracks the interior so
    # it spans the box; children shift down by a header strip.
    index_nd = folder.get("index_page")
    header_strip = 0.0
    if index_nd is not None:
        interior_w = max(interior_w, float(_NODE_W_MIN))
        index_nd["_render_width"] = interior_w
        header_strip = _NODE_H + _FOLDER_GAP

    outer_w = interior_w + 2 * pad
    outer_h = interior_h + header_strip + label_pad + pad

    positions: dict = {}
    boxes: list = []
    if not is_root:
        boxes.append({
            "path": folder["path"], "name": folder["name"], "depth": depth,
            "x": 0.0, "y": 0.0, "w": outer_w, "h": outer_h,
        })

    if index_nd is not None:
        positions[index_nd["doc_id"]] = (pad + interior_w / 2.0, label_pad + _NODE_H / 2.0)

    for key, (ix, iy) in placed.items():
        offx, offy = ix + pad, iy + label_pad + header_strip
        kind, ident = key
        if kind == "page":
            pw, ph = _page_size(page_nodes[ident])
            positions[ident] = (offx + pw / 2.0, offy + ph / 2.0)
        else:  # subfolder
            spos, sboxes, _, _ = sub_results[ident]
            for d_id, (scx, scy) in spos.items():
                positions[d_id] = (scx + offx, scy + offy)
            for b in sboxes:
                nb = dict(b)
                nb["x"], nb["y"] = b["x"] + offx, b["y"] + offy
                boxes.append(nb)

    return positions, boxes, outer_w, outer_h


def _folder_color(path: str) -> str:
    """Stable preset ("1".."6") keyed on the TOP-level folder so a subtree shares a hue."""
    top = path.split("/")[0] if path else ""
    return str(int(hashlib.md5(top.encode("utf-8")).hexdigest(), 16) % 6 + 1)


def _group_node(b) -> dict:
    """A TzaraCanvas `group` node (background rectangle) for one folder box."""
    return {
        "id": "g" + hashlib.md5(b["path"].encode("utf-8")).hexdigest()[:12],
        "type": "group",
        "x": int(round(b["x"])),
        "y": int(round(b["y"])),
        "width": int(round(b["w"])),
        "height": int(round(b["h"])),
        "label": b["name"],
        "color": _folder_color(b["path"]),
    }


def build_folder_canvas(nodes, edges) -> dict:
    """Global view: nest pages in labeled folder boxes, draw wikilinks as edges.

    Group nodes are emitted first so they render behind the page nodes.
    """
    if not nodes:
        return {"nodes": [], "edges": []}

    tree = _folder_tree(nodes)
    _attach_folder_notes(tree)
    positions, boxes, _, _ = _pack_folder(tree, 0)

    out_nodes = [_group_node(b) for b in boxes]
    id_map: dict[str, str] = {}
    centers: dict[str, tuple[float, float]] = {}
    by_id = {n["doc_id"]: n for n in nodes}
    for doc_id, (cx, cy) in positions.items():
        out_nodes.append(_page_node(by_id[doc_id], cx, cy))
        id_map[doc_id] = _node_id(doc_id)
        centers[doc_id] = (cx, cy)

    return {"nodes": out_nodes, "edges": _edges_json(edges, id_map, centers)}


def _build_tag_graph(doc_ids, vault_id: str | None = None):
    """Synthesize tag pseudo-nodes + doc->tag edges from `document_tags`.

    Returns (tag_nodes, tag_edges) over only the given `doc_ids` (scoped to vault when
    set). Tag nodes use a synthetic doc_id `#tags/<tag>` so the global folder view
    nests them under a `#tags` box for free (`_folder_tree` splits on `/`); in the
    radial local view the key is opaque. Node degree = how many of these docs carry the
    tag, so popular tags read as bigger. On any DB error returns ([], [])."""
    if not doc_ids:
        return [], []
    try:
        conn = _get_pg_connection()
    except Exception as e:  # pragma: no cover - environment dependent
        logger.warning("graph_canvas: tag query could not connect: %s", e)
        return [], []
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT doc_id, tag FROM document_tags WHERE doc_id = ANY(%(ids)s)
               AND (%(vault_id)s IS NULL OR vault_id = %(vault_id)s)""",
            {"ids": list(doc_ids), "vault_id": vault_id},
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    by_tag: dict[str, list[str]] = {}
    for r in rows:
        by_tag.setdefault(r["tag"], []).append(r["doc_id"])

    tag_nodes = []
    tag_edges = []
    for tag, docs in by_tag.items():
        key = "#tags/" + tag
        tag_nodes.append({
            "doc_id": key, "tag": tag, "_is_tag": True, "degree": len(docs),
        })
        for d in docs:
            tag_edges.append({"source": d, "target": key, "edge_type": "tag"})
    return tag_nodes, tag_edges


# Extensions whose unresolved targets aren't "pages to create" -- a dangling
# `![[Board.canvas]]` is a missing canvas/drawing, not a wiki page you'd author
# from the graph, so it doesn't earn a to-do node.
_MISSING_SKIP_EXTS = (".canvas", ".excalidraw")


def _build_missing_graph(doc_ids, vault_id: str | None = None):
    """Synthesize "missing page" pseudo-nodes from *unresolved* wikilink edges.

    An unresolved edge (resolved=FALSE, target_doc_id=NULL, target_title set) is a
    link to a page that has no file -- a "yet to be created" target. These have no
    documents row, so unlike deletion ghosts they were never rendered. We group
    every unresolved edge whose SOURCE is in `doc_ids` by wikilink_key(target) so
    all the links to the same missing title collapse to ONE node, then draw an edge
    in from each linking page. That inbound fan-in is the point: it shows how many
    (and which) pages are waiting on that page to exist.

    Distinct from deletion ghosts by design (see color comments): a missing node is
    a to-do (create it); a ghost is a broken reference (the *linking* pages need
    their now-dangling link removed or the page recreated).

    Node `doc_id` is a synthetic `__missing__/<key>` layout key (opaque in the
    organic view; it can't collide with a real vault-relative path). `missing_path`
    carries the real target path so the node links to /wiki/{vault}/<path>, which
    opens the editor in create mode. On any DB error returns ([], [])."""
    if not doc_ids:
        return [], []
    try:
        conn = _get_pg_connection()
    except Exception as e:  # pragma: no cover - environment dependent
        logger.warning("graph_canvas: missing-link query could not connect: %s", e)
        return [], []
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT source_doc_id, target_title FROM edges
               WHERE resolved = FALSE AND target_doc_id IS NULL
                 AND target_title IS NOT NULL AND target_title <> ''
                 AND source_doc_id = ANY(%(ids)s)
                 AND (%(vault_id)s IS NULL OR vault_id = %(vault_id)s)""",
            {"ids": list(doc_ids), "vault_id": vault_id},
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    # key -> {"title": display path, "sources": set(doc_id)}
    by_key: dict[str, dict] = {}
    for r in rows:
        raw = r["target_title"] or ""
        # Strip an Obsidian `|alias`/`|dimension` suffix (embeds keep it in
        # target_title because EMBED_RE, unlike WIKILINK_RE, captures the alias),
        # then drop any `#heading` fragment -- it's an intra-page anchor, so the
        # target is the base page; a bare `#anchor` link (empty base) targets the
        # current page and isn't a missing page at all.
        base = raw.split("|", 1)[0].split("#", 1)[0].strip().lstrip("/")
        if not base:
            continue
        if base.lower().endswith(_MISSING_SKIP_EXTS):
            continue
        key = wikilink_key(base)
        if not key:
            continue
        entry = by_key.setdefault(key, {"title": base, "sources": set()})
        entry["sources"].add(r["source_doc_id"])

    missing_nodes = []
    missing_edges = []
    for key, entry in by_key.items():
        node_id = "__missing__/" + key
        missing_nodes.append({
            "doc_id": node_id,
            "title": entry["title"],
            "_is_missing": True,
            "missing_path": entry["title"],
            "degree": len(entry["sources"]),
        })
        for d in entry["sources"]:
            missing_edges.append({"source": d, "target": node_id, "edge_type": "wikilink"})
    return missing_nodes, missing_edges


def build_canvas(root_doc_id: str | None = None, depth: int = 1,
                 include_isolated: bool = False, tags: bool = False,
                 vault_id: str | None = None) -> dict:
    """fetch + layout + serialize. Global (root_doc_id=None) -> force-directed
    organic web of ALL linked pages (no folder boxes; nodes colored by state);
    local -> radial link-neighborhood layout. With `tags`, overlays `#tag` nodes
    connected to the pages that carry them. Returns canvas JSON dict.

    The folder-grouped layout (`build_folder_canvas` + helpers) is kept for
    reference/tests but is no longer the global default -- the whole-vault view
    is now one organic graph (`layout_graph`: spring per component, components
    shelf-packed), matching the local view's serializer.
    """
    # Make the vault available to _doc_href for this build (thread-local).
    _href_ctx.vault = vault_id or DEFAULT_VAULT

    if root_doc_id is None:
        # Show orphans too -- they shelf-pack as their own singletons after the
        # connected clusters (Obsidian shows unlinked notes in the global graph).
        nodes, edges = fetch_graph(root_doc_id=None, include_isolated=True, vault_id=vault_id)
        # Synthetic overlays derive from the *real* pages, so snapshot their ids
        # before appending pseudo-nodes.
        real_ids = [n["doc_id"] for n in nodes]
        mn, me = _build_missing_graph(real_ids, vault_id=vault_id)
        nodes, edges = nodes + mn, edges + me
        if tags:
            tn, te = _build_tag_graph(real_ids, vault_id=vault_id)
            nodes, edges = nodes + tn, edges + te
        positions = layout_graph(nodes, edges)
        return to_canvas_json(nodes, edges, positions)

    nodes, edges = fetch_graph(root_doc_id=root_doc_id, depth=depth,
                               include_isolated=include_isolated, vault_id=vault_id)
    real_ids = [n["doc_id"] for n in nodes]
    mn, me = _build_missing_graph(real_ids, vault_id=vault_id)
    nodes, edges = nodes + mn, edges + me
    if tags:
        tn, te = _build_tag_graph(real_ids, vault_id=vault_id)
        nodes, edges = nodes + tn, edges + te
    for n in nodes:
        if n["doc_id"] == root_doc_id:
            n["_is_root"] = True
    positions = layout_radial(nodes, edges, root_doc_id)
    return to_canvas_json(nodes, edges, positions)
