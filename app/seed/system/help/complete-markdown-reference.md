---
Title: complete-markdown-reference
Date: 2026-07-16 00:07:16.173195+00:00
Tags: markdown, syntax, extensions, attributes, tables, footnotes, latex, admonitions
Summary: The document is a complete cheat‑sheet of every markdown feature Tzara supports, covering basic syntax (headings, links, images, tables, lists, code fences, LaTeX) and advanced extensions such as comments, attribute lists, page embeds, footnotes, Jupyter code blocks, smarty‑pants typographic replacements, and both Tzara and Obsidian‑style admonitions/callouts. Each feature is illustrated with example markup and its rendered output, and references to configuration via the python‑markdown extensions are provided.
---

This document is a complete reference sheet for every markdown syntax that Tzara supports.  And that I can remember.  Examples of markdown typically precede the result of what it generates.  Deviations from this pattern should be obvious. 

Some of these features are from the extensions found in the `python-markdown` library.  See <https://python-markdown.github.io/extensions> for more details, and if you want to mess with stuff, look in `src/doctransform.py` for config information.

You can add a table of contents anyware in the page, just add `[TOC]` somewhere, like so:

[TOC]

# Comments
Comments can be added to your pages with double percent signs surrounding text inline or as a fenced block of text.

`````markdown
%% This is a comment, inline. %%
This is visible! since it's between comments.
%%
This is a block comment.
Everything here gets removed form the document.

```jupyter
print("this is not included since it's commented out")
```

Nothing to see here.
%%
`````

Text above looks like this:
%% This is a comment, inline. %%
This is visible! since it's between comments.
%%
This is a block comment.
Everything here gets removed form the document.

```jupyter
print("this is not included since it's commented out")
```

Nothing to see here.
%%



# Headings

Headings are prefixed with hash marks.  You can have to 6. 

The following markdown snippit produces the results immediatly after. 
```markdown
# header 1
## header 2
### header 3
#### header 4
##### header 5
###### header 6
####### header 7
```
# header 1
## header 2
### header 3
#### header 4
##### header 5
###### header 6
####### header 7

# Linking and Embedding

#### Web Links
You can link to web pages with display text in square brackets first, then the URL in parentheses after. 
```markdown
[Tzara](https://tzara.studio/)
```
[Tzara](https://tzara.studio/)

You can also use angle brackes around a URL.
```markdown
<https://tzara.studio/>
<mailto:no-reply@example.com>
```
<https://tzara.studio/>
<mailto:no-reply@example.com>

#### Named Links
Similar to the Footnotes syntax below but without the `^` in that begins the link text in brackets. You can create a named reference to a link, and reuse that named link throughout the document.  You define the link only once. Unlike the Footnotes variant, there is no list comperable to the footnotes at the bottom of a page.
```markdown
[tzara]: https://tzara.studio/ "Optional title for named link"

See [tzara] for details.  This is another [tzara] link.
```

[tzara]: https://tzara.studio/ "Optional title for named link"

See [tzara] for details.  This is another [tzara] link.

#### Embeds
There are **two** embedding syntax examples here. One is `![ ]( )` and the other is `![[ ]]`.

An image can be embedded using the same square brakets of text and parentheses of URL, but with an exclemation mark in front. See Attributes elsewhere in the document.
```markdown
![favicon](/favicon.ico)
![favicon](/favicon.ico){: .img-center style="width:32px;"}
```
![favicon](/favicon.ico)
![favicon](/favicon.ico){: .img-center style="width:32px;"}

------------------------------------------------------------

You can also embed a canvas file, but the syntax is a little different.  The following line embeds a canvas element, and you can optionally specify the **height**, or accept a default of 420 pixels. 
`![[architecture.canvas|200]]` yields this
![[architecture.canvas|200]]
`![[architecture.canvas]]` results in the following
![[architecture.canvas]]

#### Including a page

Put an embed on a line by itself to pull another page's content in and render it in a framed box:

```markdown
![[Some Page]]              - include the whole page
![[Some Page#Overview]]     - include just that heading's section
![[Some Page#^blk1]]        - include a single block (its ^blk1 marker)
```

- The target is resolved the same way `[[wikilinks]]` are (by name, nearest   match wins), so you rarely need the folder path.
- Includes nest: an included page can itself include others. Nesting is capped (3 levels by default) and a page that includes itself in a loop is caught - either shows a small notice instead of looping forever.
- A missing page or section shows a notice rather than silently disappearing.
- Only markdown pages are included this way. `![[image.png]]`, `![[data.csv]]`, `![[report.pdf]]`, and `![[Board.canvas]]` still render as their own preview.

# Quotes

```markdown
> This is some quoted text.
>
> Additionaly, you can have nested quotes.
> > For example, this is nested
> > > And this line too!
```

> This is some quoted text.
>
> Additionaly, you can have nested quotes.
> > For example, this is nested
> > > And this line too!

# Abbreviations

You can define abbreviations in your document, and then any use of that word or phrase will allow a hover text when you mouseover it. 
```markdown
*[WWW]: World Wide Web
*[HTML]: HyperText Markkup Language
*[LOL]: Laughs Out Loud
*[setext]: Structure Enhanced Text
```
*[WWW]: World Wide Web
*[HTML]: HyperText Markkup Language
*[CSS]: Cascading Style Sheets
*[LOL]: Laughs Out Loud
*[setext]: Structure Enhanced Text
*[hello world]: Hello World, how are you?

The markdown shown above in the fenced code block immediately has the same code appear right after it, and before this block of text.  Those lines don't appear in the output anywhere.  However, if I type WWW or HTML, you can hover over them for text. You can even use phrases, hello world.

# Attribute Lists

Attribute lists allow you to add some attributes to the resulting HTML that gets created from the processed markdown.  Consider the following markdown listing and the result right after. 
```markdown
This is a paragraph. 
{: #an_id .a_class }
```
This is a paragraph. 
{: #an_id .a_class }

Notice the second line stuff in curly braces is not displayed.  However, if you right click on the "This is a paragraph." and use your browser's page inspector, you'll see that the resulting html has an `id` and `class` defined.  The raw HTML looks like this in Firefox, 
```html
<p class="a_class" id="an_id">This is a paragraph. <br></p>
```

The utility of giving items in your page a class or id value, you can customize your CSS for special styles, or even use a block of python code in a `jupyter` code fence to manipulate elements on the page dynamically. 

The `id` and `class` have the shorthand `#` and `.` leading characters.  You can also specify HTML property explictly, like so: 

```markdown
This is a paragraph with custom attributes. The id and class are explicit, and I can even alter the style of the paragraph.
{: id="example_paragraph_2" class="callout" style="border-style:dotted;" }
```

This is a paragraph with custom attributes.  The id and class are explicit, and I can even alter the style of the paragraph.
{: id="example_paragraph_2" class="callout" style="border-style:dotted;" }

A setext style header
=====================

The heading above was created by a string of equal signs just under the text, like so:
```markdown
A setext style header
=====================
```
It's an alternative way to write headers.  However, with the equal sign, `=` and the hyphen `-`, there are only two heading styles.  The dash looks like this. 

Second level setext header
--------------------------

So, that's a second level heading, written a different way, like so. 
```markdown
Second level setext header
--------------------------
```

### Hash style header ######################################

This "hash style header" just above has a long string of hash marks at the end.  When looking at a document full of text, the long line of # marks are visually striking and really standout as a heading.  You can edit the page and see for yourself, but it's just this:
```markdown
### Hash style header ######################################
```

# Attribute Lists Part 2 { title="Custom title Text showing attributes" }

The attributes mentioned above can be applied to various things.  You saw attributes being applied on a paragraph having a line just after.  Attributes can also be applied to other markdown as well. 

In the following (and above) examples, the header has a custom title attribute applied, the link below has two custom classes and a title, and in the table the **space** before the curly bracket is the difference between setting attributes on the `<TD>` tag of the HTML table, and the emphasis `<EM>` tag of the letter "b".

```markdown
# Attribute Lists Part 2 { title="Custom title Text showing attributes" }


[Example link, hover over me!](http://tzara.studio){: class="foo bar" title="Some title!" }

| set on td         | set on em        |
|-------------------|------------------|
| *a* { .foo .bar } | *b*{ .foo .bar } |

```
[Example link, hover over me!](http://tzara.studio){: class="foo bar" title="Some title!" }

| set on td    | set on em   |
|--------------|-------------|
| *a* { .foo .bar } | *b*{ .foo .bar } |

# Definition Lists

Perhaps you need to define some terms for a document.  This the syntax for definition lists, the word, then on the next line a colon, a mininum of one space, and then the definition, and finally an empty row after. 

```markdown
Apple
:   Pomaceous fruit of plants of the genus Malus in
    the family Rosaceae.

Orange
:   The fruit of an evergreen tree of the genus Citrus.

Grape
:   The small, sweet fruit that grows in bunches on vines, belonging to the genus Vitis in the family Vitaceae.
```
And the results are, 

Apple
:   Pomaceous fruit of plants of the genus Malus in
    the family Rosaceae.

Orange
:   The fruit of an evergreen tree of the genus Citrus.

Grape
:   The small, sweet fruit that grows in bunches on vines, belonging to the genus Vitis in the family Vitaceae.


# Fenced Code Blocks
A fenced code block is defined with starting a line with 3 or more backticks and, on a subsequent line, the same number of backticks.  You can also use 3 or more tilde, ~, characters to build a fence around code or whatever you're typing.  

Typing this, with three backtics at the start and also at the end, 
`````markdown
```
print("hello world, a single line of code")
```
`````
will give you the following:
```
print("hello world, a single line of code")
```

You can also use the tilde, but backtics seem to have better support. 
`````markdown
~~~
print("Using a ~ works, but it might break some stuff.")
~~~
`````

~~~
print("Using a ~ works, but it might break some stuff.")
~~~

You can also specify the language on the first line like so:
`````markdown
``` python
# this is a python block, with language explicitly stated
if True:
    def x(y): 
        return y * 2
```
`````
Will turn into this:
``` python
# this is a python block, with language explicitly stated
if True:
    def x(y): 
        return y * 2
```

## Jupyter Code Blocks

A special language that Tzara recognizes for fenced code blocks is "Jupyter".  If you use this around some Python code, the block will become executable on the page similar Jupyter Notebook.  See [jupyter](jupyter.md) for more details and examples. 

`````markdown
``` jupyter
print("hello world")
```
`````

``` jupyter
print("hello world")
```

# Footnotes

Footnotes[^3] have three components.  A name, a reference, and a definition.  The reference[^webster] appears inline in your writting, and is square brackets with a carret inside and a number ow word as a lable,  `[^7]` or `[^roget]`.  The label you use is for you to remember when writing, the order is in which they appear in the document determines their numerical ordering at the end. The content of the footnote, that is the name and definition, can appear near where they're used.  The name is the number or text you use in your inline references, and the definition is what you want to be in the footnote.  The definition body is **indented 4 spaces** if you want a block of text, otherwise it can appear right after the name.  The footnotes will be at the bottom of the document, however, you can use the phrase "Footnotes Go Here"[^1] but with triple forward slashes on either side, and on a line by itself some place else other than the bottom if you want to relocate them.  

[^3]: This is a footnote, and the label changes because it's referenced first. 
[^webster]: 
    This is a block example.  

    ```python
    print("I can even have a block of code here!")
    ```
    *And* you easily **style** your footnotes. 
[^1]: Putting `///Footnotes Go Here///` in the paragraph messed it up, but that's the phrase you can place just about anywhere on the page to place footnotes in that location.  Not sure why you would need that, but you can.

# LaTeX

You can specify LaTeX in your page using two variations of inline and two variations of block LaTeX.  If LaTeX is detected on a page, then some [KaTeX](https://katex.org/) javascript is used using the content distribution network `jsdeliver.net`. 

```markdown
Some inline formula examples,  $E=mc^{2}$, and \( F=ma \).
And some block examples:
$$ e^{i \pi} + 1 = 0$$
and also \[ e^{x} = \sum^{\infty}_{n=0} \frac{x^{n}}{n!} \].
```

Some inline formula examples,  $E=mc^{2}$, and \( F=ma \).
And some block examples:
$$ e^{i \pi} + 1 = 0$$
and also \[ e^{x} = \sum^{\infty}_{n=0} \frac{x^{n}}{n!} \].

# Tables

See <https://michelf.ca/projects/php-markdown/extra/#table> for more info.

First, the raw markdown, and then the results right after. In the second example, the second row has some colon delemiters on either left, right, or both, just under their respetive table headers, and this determines if the contents of the cell are left aligned, right aligned, or centered. 

```markdown
First Header  | Second Header
------------- | -------------
Content Cell  | Content Cell
Content Cell  | Content Cell

| Item      | Value | Returnable  |
| --------- | -----:|:-----------:|
| Computer  | $1600 | maybe?      |
| Phone     |   $12 | no          |
| Orange    |    $1 | no          |
| Mouse     |   $12 | yes         |
| keyboard  |    $1 | possibly    |
```

First Header  | Second Header
------------- | -------------
Content Cell  | Content Cell
Content Cell  | Content Cell

| Item      | Value | Returnable  |
| --------- | -----:|:-----------:|
| Computer  | $1600 | maybe?      |
| Phone     |   $12 | no          |
| Orange    |    $1 | no          |
| Mouse     |   $12 | yes         |
| keyboard  |    $1 | possibly    |

# Smarty Pants

This replaces certain characters or sequences of characters with some other glyph.  The list below is what's currently configured.  This is an extension to the python-markdown <https://python-markdown.github.io/>, so for more information, see [Smarty Pants Reference](https://python-markdown.github.io/extensions/smarty/). If you are interested in reconfiguring or turning this off, you can find settings for this, and others, in `src/doctransform.py`.

The before and after,
```markdown
'single quote'
"double quote"
<< angled brackets >>
an ellipse...
n--dash
m---dash
```

1. 'Single quote'
2. "double quote"
3. << angled brackets >>
4. an ellipse...
5. n--dash
6. m---dash

# Lists 

Lists can be ordred or unordered, and also have optional checkmarks.  The lists are successive lines where the first thing is either a number followed by a dot, `1.`, or a dash `-` or asterisk `*`.  Checked items are denoted by following the initial marker with a space and either an empty pair of square brackes with a space between then, `[ ]`, or `[x]` to mark the item with a checkmark.     

See [Sane Lists Reference](https://python-markdown.github.io/extensions/sane_lists/) for slight variations on how lists get handled. 

For ordered lists, you can repeat the same number or start counting. 

This source produces the list just after. *Note*, there are two lines seperating the lists, otherwise they'll join together.  
```markdown
1. One
2. Two
3. Three
```
1. One
2. Two
3. Three

```markdown
1. One
1. Two
1. Three
```
1. One
1. Two
1. Three

```markdown
* Item 1
* Item 2
* Item 3
```
* Item 1
* Item 2
* Item 3

```markdown
- Item 1 and dashes are dots!
- Item 2
    - [x] Indented item, and x's are checks!
    - [ ] Another indented item
    - This is not an item to check
- Item 3
```
- Item 1 and dashes are dots!
- Item 2
    - [x] Indented item, and x's are checks!
    - [ ] Another indented item
    - This is not an item to check
- Item 3


# Markdown in HTML

Sometimes markdown just does not get the job done and you have to resort on HTML for something.  If you need to write some HTML and then want to mix some Markdown syntax with it, you have specify `markdown="1"` inside any tag you want internal markdown to handled. 

```HTML
<div>
This is a div tag with *italic* and **bold** markdown. 
</div>
<div markdown="1">
This is a div tag with *italic* and **bold** markdown, but this time, there is a markdown directive in the HTML tag.
</div>
```

<div>
This is a div tag with *italic* and **bold** markdown. 
</div>
<div markdown="1">
This is a div tag with *italic* and **bold** markdown, but this time, there is a markdown directive in the  tag.
</div>

# Adominitions and Callouts

Different syntax and different names for the same thing.   

The first syntax is three exclamation poins, `!!!`, followed by a type or style fromt the list below, a title in double quotation marks, and then content is an indented body starting on the next line.

```markdown
!!! attention "This is an admonition"
    The following styles can be used, `attention`, `caution`, `danger`, `error`, `hint`, `note`, `tip`, `warning`

```
!!! attention "This is an admonition"
    The following styles can be used, `attention`, `caution`, `danger`, `error`, `hint`, `note`, `tip`, `warning`

Obsidian syntax is blockquoted with `>` on every line, and the first line has the style and title written as `[!attention]` followed by the title on the same line. This format is a little easier to include blank lines because they're blockquoted, so not really an empty line.  

```markdown
> [!attention] This is a callout
> This is the body of the callout.  This is styled the same as the admonition, and the styles are largely the same. Obsidian has the following: abstract, summary, tldr, info, todo, tip, hint, important, success, check, done, question, help, faq, warning, caution, attention, failure, fail, missing, danger, error, bug, example, quote. 
> 
> This is still in the obsidian style callout. 
```

> [!attention] This is a callout
> This is the body of the callout.  This is styled the same as the admonition, and the styles are largely the same. Obsidian has the following: abstract, summary, tldr, info, todo, tip, hint, important, success, check, done, question, help, faq, warning, caution, attention, failure, fail, missing, danger, error, bug, example, quote. 
> 
> This is still in the obsidian style callout. 

These can be nested in each other.  The Obsidian syntax allows for having collapsable containers, either fully expanded or initially collapsed. 

```markdown
> [!danger]+ Expanded
> This is a test.
> > [!tldr]+ Too long did not read
> > This is a callout inside another
> > > [!check]- This is collapsed
> > > And is three levels deep
```

> [!danger]+ Expanded
> This is a test.
> > [!tldr]+ Too long did not read
> > This is a callout inside another
> > > [!check]- This is collapsed
> > > And is three levels deep

```markdown
!!! tip "Can this be a container"
    Yes it can! 
    !!! danger "Inner item"
        This is inside
        !!! caution "this work?"
            And this is three levels deep
```

!!! tip "Can this be a container"
    Yes it can! 
    !!! danger "Inner item"
        This is inside
        !!! caution "this work?"
            And this is three levels deep.  