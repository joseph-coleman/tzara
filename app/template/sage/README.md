# Sage - a full-width, pastel-green theme

A second worked example (see `ocean/` for the color-only one). Sage does two things from a single [`theme.css`](theme.css), copying no `.html`:

- **Color** - overrides the four seed tokens for a muted, pastel green with a terracotta link color. This part is drift-free (Tier 1). 
- **Layout** - a handful of CSS rules turn the centred ~800px column into a   full-width page, drop the sidebar table of contents, and move each page's   tags + summary to a strip at the bottom. This is Tier 2: it targets the ids/classes the default template emits (`#document`, `#toc`, `.toc`,   `#search-page`, `.sidebar-*`).

Activate it:

```
TZARA_TEMPLATE=sage
```

## How the full-width trick works

The default centres `#document` (`width: min(80%, 800px); margin: auto`) and
floats a faint `#toc` in the left gutter. Sage:

1. Makes `<body>` a **flex column** so the children can be reordered, then sets
   `order:` so the tags/summary block (`#toc`) lands *after* the content and the footer stays last. Canvas/graph pages use `body.fullwidth`, which has its own higher-specificity layout and no `#document`/`#toc`, so they're untouched.
2. Gives `#document` (and `#search-page`) `width:auto; max-width:none` with `clamp()` side gutters, so content uses the whole width on any screen.
3. Un-floats `#toc`, hides the `.toc` nav inside it, and flex-lays the surviving tags + summary along the bottom.

## The drift trade

Color tokens can't go stale. The layout rules *can*, in one narrow way: if a future Tzara release renames a layout hook (`#document`, `#toc`, `.toc`, `#search-page`, `.sidebar-*`), re-check the "Full-width layout" section of `theme.css`. Nothing is copied, so there's no menu or feature to fall behind - only those selector names to keep matching. That's the deal for restyling layout instead of only color.
