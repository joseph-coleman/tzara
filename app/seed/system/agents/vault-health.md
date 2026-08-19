---
type: agent
description: Demonstrates composing the wiki read/analysis methods into single robust tools
vaults: main
mode: propose
output: Vault Health.md
max_iterations: 6
log: true
Tags: wiki, orphan-pages, link-proposal, health-report, metadata, tagging
Summary: The prompt instructs you to run a single vault health report, then, if any orphan pages exist, stage link proposals for up to five of them using the `link_orphans_to_best_match` tool, and finally produce a brief markdown summary of the findings and suggested links. No additional code or commentary should be included—only the markdown report.
---

# Prompt

You maintain a tidy wiki. You have two tools, each of which does a whole job in
one call — you do NOT need to chain smaller steps yourself:

- `vault_health_report()` — returns a text summary of the vault's size, orphan
  pages, most-linked hubs, and tag spread. Call it to understand the vault.
- `link_orphans_to_best_match(max_links)` — finds orphan pages (no links in or
  out) and, for each, proposes a wikilink to the most semantically similar page.
  Proposals are STAGED for human review; nothing is applied without approval.

Do this:
1. Call `vault_health_report()` once.
2. If there are orphans, call `link_orphans_to_best_match(5)` once.
3. Write a short markdown report of what you found and what you proposed.

Output ONLY the report markdown. No preamble, no code fences, no commentary.

# Kickoff

Assess the vault's health and propose links for up to 5 orphan pages, then write your report.

```python
def vault_health_report() -> str:
    """One-call vault health summary: page count, orphan count, top link hubs,
    and tag spread — composed in Python from the raw metadata tables."""
    import pandas as pd

    docs = pd.DataFrame(wiki.queryDocuments())
    edges = pd.DataFrame(wiki.queryEdges())
    tags = pd.DataFrame(wiki.queryDocumentTags())

    n_docs = 0 if docs.empty else len(docs)
    lines = [f"Pages: {n_docs}"]

    # In-degree hubs: how many resolved links point AT each page.
    if not edges.empty:
        resolved = edges[edges["resolved"] == True]  # noqa: E712
        if not resolved.empty:
            indeg = resolved["target_doc_id"].value_counts().head(5)
            lines.append("Top hubs (most backlinks): "
                         + ", ".join(f"{d} ({c})" for d, c in indeg.items()))
        unresolved = int((edges["resolved"] == False).sum())  # noqa: E712
        lines.append(f"Unresolved links (ghost targets): {unresolved}")

    # Orphans: pages with no link in or out (uses the shared analysis view).
    orphans = pd.DataFrame(wiki.list_orphans())
    lines.append(f"Orphan pages: {0 if orphans.empty else len(orphans)}")

    if not tags.empty:
        top_tags = tags["tag"].value_counts().head(5)
        lines.append("Top tags: " + ", ".join(f"#{t} ({c})" for t, c in top_tags.items()))

    return "\n".join(lines)


def link_orphans_to_best_match(max_links: int = 5) -> str:
    """For each orphan page, search for its closest relative and STAGE a wikilink
    proposal. One deterministic call replaces a fragile find->search->write chain."""
    import pandas as pd

    orphans = pd.DataFrame(wiki.list_orphans())
    if orphans.empty:
        return "No orphan pages found; nothing to link."

    staged = []
    for doc_id in orphans["doc_id"].head(max_links):
        hits = wiki.search(str(doc_id).rsplit("/", 1)[-1].replace(".md", ""), top_k=3)
        target = next((h["doc_id"] for h in (hits or [])
                       if h.get("doc_id") and h["doc_id"] != doc_id), None)
        if not target:
            continue
        body = wiki.read(doc_id) or ""
        new_body = body.rstrip() + f"\n\n## Related\n- [[/{target.rsplit('.', 1)[0]}]]\n"
        wiki.write(doc_id, new_body, note=f"link orphan {doc_id} -> {target}")
        staged.append(f"{doc_id} -> {target}")

    if not staged:
        return "Found orphans but no confident match to link; staged nothing."
    return "Staged link proposals:\n" + "\n".join(f"- {s}" for s in staged)
```
