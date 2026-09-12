---
title: Markdown Syntax
Summary: Quick reference for basic markdown syntax.
GenerateMetadata: false
---

* [help](../help.md)
    * [configurations](configurations.md) - How (and where) to configure things.
    * [basics](basics.md) - The basics of using Tzara.
    * **markdown-syntax** - The markdown you’ll use every day, shown by example.
        * [complete markdown reference](complete-markdown-reference.md) Even more markdown examples.
    * [frontmatter](frontmatter.md) - The metadata block at the top of a page, and what Tzara does with it.
    * [jupyter](jupyter.md) - Jupyter integration details and examples.
    * [agents](agents.md) - Creating agents and how they work
    * [editors](editors.md) - Custom “/” menu commands that transform text as you edit

# Markdown Syntax by Example

Every page in Tzara is markdown. Here's the syntax you'll use most, each shown as the source you'd type followed by what it produces.

To see a full breakdown of everything supported by Tzara, see [complete markdown reference](complete-markdown-reference.md).

## Headings

```
# Heading 1
## Heading 2
### Heading 3
```

# Heading 1
## Heading 2
### Heading 3

## Emphasis

```markdown
*italic*, **bold**, ***bold italic***, 
~~strikethrough~~, ==highlight==, `inline code`
```

*italic*, **bold**, ***bold italic***, 
~~strikethrough~~, ==highlight==, `inline code`

## Lists

```markdown
- unordered item
- another
    - nested item, needs 4 spaces

1. ordered item
2. second item
```

- unordered item
- another
    - nested item, needs 4 spaces

1. ordered item
2. second item

## Links

```markdown
[External link](https://example.com)
[[Another Page]]              - Obsidian-style wikilink (resolves within the vault)
[[Another Page|shown text]]   - wikilink with custom display text
```

Wikilinks resolve across the whole vault by page name, so you can link to a note
without knowing which folder it lives in.

### Named Links
```
[tzara]: https://tzara.studio/ "Optional title for named link"

See [tzara] for details.
```

[tzara]: https://tzara.studio/ "Optional title for named link"

See [tzara] for details.

## Including a page

```markdown
![[Some Page]]              - include the whole page
![[Some Page#Overview]]     - include just that heading's section
```
Up to 3 levels deep. 

## Images and attachments

```markdown
![Alt text](my-diagram.png)
![[embedded-note]]            - includes another page inline (see below)
```

### Aligning an image

Markdown has no alignment syntax of its own, but you can attach a helper
class to any standard `![](...)` image with a trailing `{: .class }` block:

```markdown
![Alt text](my-diagram.png){: .img-center }
![Alt text](my-diagram.png){: .img-right }
![Alt text](my-diagram.png){: .img-float-right }
![Alt text](my-diagram.png){: .img-float-left }
```

- `.img-center` / `.img-right` place the image on its own line, centered or
  pushed to the right margin.
- `.img-float-left` / `.img-float-right` let following text wrap alongside it.

These classes work on the `![](...)` form, not the `![[...]]` embed form.

## Blockquotes and rules

```markdown
> A quote, which can span
> multiple lines.

---
```

> A quote, which can span
> multiple lines.

---

## Tables

```markdown
| Feature   | Supported |
|-----------|:---------:|
| Tables    | ✅        |
| Wikilinks | ✅        |
```

| Feature   | Supported |
|-----------|:---------:|
| Tables    | ✅        |
| Wikilinks | ✅        |

## Code blocks

Fence a block with triple backticks and an optional language for highlighting:

```python
def greet(name):
    return f"Hello, {name}!"
```

To make a code block **executable**, see [jupyter examples](jupyter/jupyter-examples.md).

## Frontmatter

A page can open with a YAML frontmatter block for metadata:

```markdown
---
title: My Note
tags: [reference, project]
---
```

A few of these keys change real behavior - keeping a page out of search, pinning tags the LLM isn't allowed to overwrite, setting the voice the editor tools write in. See [frontmatter](frontmatter.md) for the full list.

## Task lists

```markdown
- [x] Ship help docs
- [ ] Write more notes
```

- [x] Ship help docs
- [ ] Write more notes
