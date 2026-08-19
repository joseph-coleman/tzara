# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import functools

import markdown

from config import EMBED_INCLUDE_MAX_DEPTH
from src.jupyter_extension import JupyterCellExtension
from src.frontmatter_extension import FrontmatterExtension
from src.mermaid_extension import MermaidExtension
from src.markdown_extensions import (
    AutoLinkExtension,
    CanvasEmbedExtension,
    FileEmbedExtension,
    HighLightExtension,
    ImageEmbedExtension,
    LaTeXExtension,
    StrikeThroughExtension,
    TranscludeEmbedExtension,
    WikiLinkExtension,
    ObsidianCalloutExtension,
    AdmonitionNormalizerExtension,
    ChecklistExtension,
    ObsidianCommentExtension,
)
from src.wikidoc import WikiDoc


class DocTransform:
    """
    This is an abstract base class for transforming wiki document.

    The idea here was to have other types of transformations besides
    the MarkdownDocTransform.  For example, it might be usefull to
    have something that converts to static html.
    """

    def __init__(self, wd: WikiDoc):
        pass

    def get_content(self):
        pass


class MarkdownDocTransform(DocTransform):
    """
    Class that takes a WikiDoc instance and transforms markdown to html.
    """

    MD_EXTENSIONS = [
        LaTeXExtension(),
        StrikeThroughExtension(),
        HighLightExtension(),
        "extra",
        # "codehilite",
        "admonition",
        "legacy_attrs",
        # "legacy_em", not good, turn _my_word_ into <em>my</em>word_  instead of <em>my_word</em>
        FrontmatterExtension(),
        "nl2br",
        "sane_lists",
        "smarty",
        "toc",
        ImageEmbedExtension(),
        # AutoLinkExtension(),
        # WikiLinkExtension(), specified below with parameters
        #
        JupyterCellExtension(),
        MermaidExtension(),
        ObsidianCalloutExtension(),
        AdmonitionNormalizerExtension(),
        ChecklistExtension(),
        ObsidianCommentExtension(),
    ]
    # these configs are only for builtin extensions,
    # config passing for custom extensions needs to occur when instatiating
    # the object
    MD_EXTENSION_CONFIG = {
        "extra": {
            "abbr": {},  # glossary: A dictionary where the ky is the abbreviation and the value is the definition.
            "attr_list": {},  # no config options
            "def_list": {},  # no config options
            "fenced_code": {},  # lang_prefix  The prefix prepended to the langauge class assigned to the HTML <code> tag.  default `language-`
            "footnotes": {
                "PLACE_MARKER": "///Footnotes Go Here///",
                "UNIQUE_IDS": False,
                "BACKLINK_TEXT": "&#8617;",
                "SUPERSCRIPT_TEXT": "{}",
                "BACKLINK_TITLE": "Jump back to footnote {} in the text",
                "SEPARATOR": ":",
                "USE_DEFINITION_ORDER": True,
            },
            "tables": {
                "use_align_attribute": False
            },  # True to use "align" instead of style attribute
            "md_in_html": {},  # markdown in html has no configs
        },
        "codehilite": {
            "linenums": True,  # True, False, None (auto), alieas for linenos
            "guess_lang": False,
            "css_class": "codehilite",
            "pygments_formatter": "html",
            # "noclasses": False,
            # "pygments_style": "default",
            "use_pygments": True,
        },
        "legacy_attrs": {},  # todo
        "nl2br": {},  # no config, treat new lines as hard breaks
        "smarty": {
            "smart_dashes": True,
            "smart_quotes": True,
            "smart_angled_quotes": True,  # default False
            "smart_ellipses": True,
            "substitutions": {
                "left-single-quote": "&lsquo;",  # sb is not a typo!
                "right-single-quote": "&rsquo;",
                "left-double-quote": "&ldquo;",
                "right-double-quote": "&rdquo;",
                # "left-single-quote": "&sbquo;",  # sb is not a typo!
                # "right-single-quote": "&lsquo;",
                # "left-double-quote": "&bdquo;",
                # "right-double-quote": "&ldquo;",
            },
        },
        "toc": {
            "marker": "[TOC]",
            "title": None,  # title to insert in the toc <div>
            "title_class": "toctitle",
            "toc_class": "toc",
            "anchorlink": False,  # True, headers link to themselves,
            "anchorlink_class": "toclink",
            "permalink": False,  # True or string to generate links at end of each header.  True uses &para;
            "permalink_class": "headerlink",
            "permalink_title": "Permanent link",
            "permalink_leading": False,  # True if permanant links should be generated
            "baselevel": 1,  # adjust header size allowed, 2 makes #5 = #6, 3 makes #4=#5=#6,
            # "slugify": callable to generate anchors
            "separator": "-",  # replaces white space in id
            "toc_depth": 6,  # bottom depth of header to include.
        },
    }

    def __init__(self, wd: WikiDoc):
        self.wd = wd

    def get_untransformed_content(self):
        return self.wd.get_content()

    def get_wiki_doc(self):
        return self.wd

    def get_content(self, use_wiki_link=True, format_code=False):
        html = self.wd.get_content()
        path = self.wd.url_pieces["path"]

        doc_data = {}

        # custom extensions need to be configured on creation,
        # and this one needs the current path
        all_extensions = MarkdownDocTransform.MD_EXTENSIONS
        if use_wiki_link:
            # Scope wikilink resolution + generated hrefs to this document's vault.
            # The per-request extensions (wikilinks, canvas/file/transclusion
            # embeds) are built by the shared helper so an embedded page's own
            # nested render gets the identical set. Seed the transclusion
            # visited-set with this page (so it can't include itself) at depth 0.
            vault = self.wd.vault()
            all_extensions = _build_wiki_extensions(
                vault, path, depth=0,
                visited=frozenset({self.wd.relative_file_path()}),
            )
        if format_code:
            #print("Hey, looks like format_code is True!!!!!")
            all_extensions = all_extensions + ["codehilite"]

        md = markdown.Markdown(
            extensions=all_extensions,
            extension_configs=MarkdownDocTransform.MD_EXTENSION_CONFIG,
            output_format="html",
        )

        html = md.convert(html)

        self.md = md

        return html

    def get_md(self):
        """
        Returns markdown object from `markdown.Markdown(...)`
        """
        if hasattr(self, "md"):
            return self.md
        return None  # this should probably throw an error.


def _build_wiki_extensions(vault, path, depth, visited):
    """The per-request markdown extensions that need the document's vault + path
    bound in (wikilinks, canvas/file/transclusion embeds). Shared by the
    top-level page render and every nested transclusion render, so an embedded
    page gets the identical feature set - and its own include recursion is
    bounded by the same depth/visited threaded through here."""
    resolve = functools.partial(WikiDoc.resolve_wikilink, vault=vault)
    return MarkdownDocTransform.MD_EXTENSIONS + [
        WikiLinkExtension(
            base_url=f"/wiki/{vault}",
            edit_url=f"/edit/{vault}",
            current_path=path,
            page_exists_callback=functools.partial(
                WikiDoc.wikilink_page_check, vault=vault
            ),
            resolve_callback=resolve,
        ),
        # Canvas embeds (![[Board.canvas]]) need the vault to build /raw and
        # /wiki URLs and the same resolver as wikilinks.
        CanvasEmbedExtension(
            vault=vault, current_path=path, resolve_callback=resolve,
        ),
        # CSV/TSV/PDF preview embeds (![[data.csv]], ![[report.pdf]]).
        FileEmbedExtension(
            vault=vault, current_path=path, resolve_callback=resolve,
        ),
        # Markdown include embeds (![[Page]], ![[Page#Heading]], ![[Page#^blk]]):
        # rendered in a framed box via render_markdown_fragment, bounded by
        # depth + visited so nested/cyclic includes terminate.
        TranscludeEmbedExtension(
            vault=vault,
            current_path=path,
            resolve_callback=resolve,
            # vault is bound in here, mirroring resolve_callback, so the embed
            # renderer only threads current_path/depth/visited per nested level.
            render_callback=functools.partial(render_markdown_fragment, vault=vault),
            depth=depth,
            visited=visited,
            max_depth=EMBED_INCLUDE_MAX_DEPTH,
        ),
    ]


def render_markdown_fragment(text, *, vault, current_path, depth, visited):
    """Render a raw markdown fragment to HTML with the full wiki extension set at
    a given include depth. Used for nested transclusion (![[Page]]): each embed
    renders as its own independent markdown pass, one level deeper.

    Returns ``(html, flags)`` where ``flags`` is the child render's truthy
    ``tzara_has_*`` feature flags (mermaid/jupyter/latex/canvas/transclusion).
    The caller bubbles these up to the host page's md so base.html loads the
    JS/CSS an embedded diagram/equation/cell needs to actually activate - the
    flags otherwise die on this throwaway child instance. Because md.convert()
    runs the child's own transclusion preprocessor synchronously, by the time we
    read them here they already include any grandchild flags, so it chains."""
    md = markdown.Markdown(
        extensions=_build_wiki_extensions(vault, current_path, depth, visited),
        extension_configs=MarkdownDocTransform.MD_EXTENSION_CONFIG,
        output_format="html",
    )
    html = md.convert(text)
    flags = {k: v for k, v in vars(md).items() if k.startswith("tzara_has_") and v}
    return html, flags
