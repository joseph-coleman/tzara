# Making your own Tzara theme

This folder (`ocean`) is a **complete, working example theme** and a template you can copy. The whole theme is a single file, [`theme.css`](theme.css), and it recolors the entire wiki without copying anything from the default theme. That is the point: a theme built this way **cannot drift** when Tzara adds a menu item, changes `tzara.css`, or restyles a page, because it doesn't duplicate any of those things.

## How theming works in Tzara

Two mechanisms, both with automatic fallback to the `default` theme:

1. **Templates** (`*.html`) are loaded by a Jinja `ChoiceLoader` (`app/main.py`): the active theme first, then `default`. A theme only needs the `.html` files it wants to change; everything else falls back. There is one Jinja environment per theme, selected per request.

2. **Assets** (`.css`, `.js`) are served by the resolver in `app/src/wikidoc.py`. A request for `/template/<THEME>/foo.css` is looked up as `template/<THEME>/foo.css`, then `template/default/foo.css`.

**The theme name in an asset URL is significant — do not hard-code it.** Write `{{theme}}`, which every template has in scope:

```html
<link rel="stylesheet" type="text/css" href="/template/{{theme}}/tzara.css" />
```

Hard-coding `/template/default/tzara.css` would pin the *default* theme's copy of that file and silently ignore your own. The name has to travel in the URL because themes are **per-vault**: a stylesheet is fetched as its own request, with no vault context, so the name is the only thing keeping two vaults' browser cache entries apart. (An unrecognized segment still falls back to the active theme, so an old cached page keeps working.)

`base.html` loads one extra stylesheet **last**:

```html
<link rel="stylesheet" type="text/css" href="/template/{{theme}}/theme.css" />
```

That is the drift-free hook. The default theme ships an empty `theme.css`; your theme ships one with overrides. Because it loads last and unlayered, its `:root` declarations win over the layered tokens in `tzara.css`.

Stylesheets are served with `Cache-Control: no-cache`, so an edit to `theme.css` shows up on a normal reload — no hard refresh needed. (Revalidation is answered with a `304`, so nothing is re-downloaded unless it actually changed.)

## Activate a theme

**Site-wide** — set the environment variable (see `.env.template`) and restart:

```
TZARA_TEMPLATE=ocean
```

**Per vault, without writing a theme at all** — a vault can override the same four seed tokens inline, from the settings panel on `/vaults` or by hand:

```json
{
  "display_name": "The Expanse",
  "colors": {
    "base": "#7c3aed",
    "background": "#f6f2fb",
    "foreground": "#0d1b24",
    "link": "#0a7d4f"
  }
}
```

These are emitted as an inline `:root` block *after* `theme.css`, so they layer on top of whatever theme the vault uses — two vaults can share a theme and still read as different places. Use a theme folder when you want to change layout, fonts or markup; use `colors` when you only want a different accent. The same light/dark polarity rule applies: keep `background` light and `foreground` dark.

**Per vault, whole theme** — add a `template` key to that vault's `.tzara/config.json`:

```json
{
  "display_name": "The Expanse",
  "template": "ocean",
  "version": 1
}
```

The per-vault value wins; vaults that set none use `TZARA_TEMPLATE`, and pages that belong to no vault (`/vaults`, `/manage/*`) always use it. A `template` naming a folder that does not exist falls back rather than erroring, so a typo cannot take a vault offline.

`default` (the built-in) needs no folder of its own.

## The three customisation levels

Pick the **lowest** level that gets you what you want -- lower levels copy less and therefore drift less.

### Level 1 -- Recolor (drift-free) -- *what `ocean` does*

Ship only `theme.css`, overriding the four seed tokens:

| Token               | Role                                                            |
| ------------------- | -------------------------------------------------------------- |
| `--base-color`      | Accent hue -- nav, buttons, callouts, canvas, all derived shades |
| `--background`      | Light-mode page surface                                        |
| `--foreground`      | Light-mode text (roles swap automatically in dark mode)        |
| `--base-link-color` | Link hue                                                       |

Everything else in `tzara.css` is derived from these with `oklch(from ...)` and recomputes automatically, in **both** light and dark mode. Do **not** add a dark-mode block -- Tzara derives dark mode from the same four seeds. Copy this folder, change four hex values, done.

### Level 2 -- Restyle or relayout (drifts a little)

Add more CSS to `theme.css` (new rules, overridden component styles, fonts), or override an individual template such as `_header.html`, `_footer.html`, or `document.html`. The `ChoiceLoader` means you copy **only** the file you change.

Drift caveat: a copied `_header.html` / `_footer.html` freezes the current menu, so a future Tzara release that adds a nav item won't show it in your copy. If you override these, plan to re-check them against `default` when you upgrade. Prefer CSS in `theme.css` over copying HTML wherever it can do the job.

### Level 3 -- Replace the stylesheet wholesale (drifts the most)

Copy `tzara.css` (and/or `tzara_code.css`) into your theme folder and edit freely. You now own ~2,500 lines and inherit every future change by hand. This is the "price of full control" -- only reach for it when levels 1-2 can't express the change.

## Quick start

```
cp -r app/template/ocean app/template/mytheme
# edit app/template/mytheme/theme.css -- change the four hex values
# set TZARA_TEMPLATE=mytheme and restart
```
