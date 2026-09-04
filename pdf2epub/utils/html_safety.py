"""Small HTML safety filter for generated EPUB content."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from lxml import html as lxml_html


_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "col",
    "colgroup", "dd", "del", "div", "dl", "dt", "em", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p",
    "pre", "q", "s", "small", "span", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
    # MathML emitted by the LaTeX conversion path.
    "annotation", "annotation-xml", "maction", "maligngroup", "malignmark",
    "math", "menclose", "merror", "mfenced", "mfrac", "mglyph", "mi",
    "mlabeledtr", "mlongdiv", "mmultiscripts", "mn", "mo", "mover",
    "mpadded", "mphantom", "mroot", "mrow", "ms", "mscarries", "mscarry",
    "msgroup", "msline", "mspace", "msqrt", "msrow", "mstack", "mstyle",
    "msub", "msup", "msubsup", "mtable", "mtd", "mtext", "mtr", "munder",
    "munderover", "semantics",
}
_DROP_TAGS = {
    "base", "button", "embed", "form", "head", "iframe", "input", "link",
    "meta", "object", "script", "style", "svg", "textarea",
}
_DOCUMENT_DROP_TAGS = {
    "base", "button", "embed", "form", "iframe", "input", "object",
    "script", "svg", "textarea",
}
_ALLOWED_ATTRIBUTES = {
    "abbr", "alt", "class", "colspan", "headers", "height", "href", "id", "lang",
    "name", "rel", "role", "rowspan", "scope", "src", "start", "summary",
    "charset", "content", "http-equiv",
    "accent", "align", "bevelled", "close", "columnalign", "columnlines",
    "columnspacing", "crossout", "denomalign", "depth", "displaystyle",
    "encoding", "fence", "height", "href", "id", "infixlinebreakstyle",
    "largeop", "length", "linethickness", "location", "lspace", "mathbackground",
    "mathcolor", "mathsize", "mathvariant", "maxsize", "minsize", "movablelimits",
    "notation", "numalign", "open", "rowalign", "rowlines", "rowspacing",
    "scriptlevel", "scriptsizemultiplier", "selection", "separator", "separators",
    "side", "stackalign", "stretchy", "subscriptshift", "supscriptshift", "symmetric",
    "style", "title", "type", "width", "xmlns", "xml:lang",
}


def _safe_url(value: str, *, attribute: str) -> bool:
    value = value.strip()
    if not value or value.startswith("//"):
        return False
    lowered = value.lower()
    if lowered.startswith(("javascript:", "vbscript:", "data:text/")):
        return False
    parsed = urlsplit(value)
    if not parsed.scheme:
        return True
    if parsed.scheme in {"http", "https"}:
        return True
    if attribute == "href" and parsed.scheme == "mailto":
        return True
    if attribute == "src" and lowered.startswith("data:image/"):
        return ";base64," in lowered and not lowered.startswith("data:image/svg")
    return False


def _safe_style(value: str) -> bool:
    lowered = value.lower().replace(" ", "")
    return not any(
        marker in lowered
        for marker in (
            "url(", "expression(", "javascript:", "vbscript:",
            "@import", "behavior:",
        )
    )


def _sanitize_tree(root, *, allowed_tags=None, drop_tags):
    """Apply the shared safety rules to an lxml HTML tree."""
    for element in list(root.iterdescendants()):
        if element.getparent() is None:
            continue
        tag = element.tag.lower() if isinstance(element.tag, str) else ""
        if tag in drop_tags:
            element.drop_tree()
            continue
        if allowed_tags is not None and tag not in allowed_tags:
            element.drop_tag()
            continue

        if tag == "style" and not _safe_style(element.text or ""):
            element.drop_tree()
            continue
        if tag == "meta" and element.get("http-equiv", "").lower() == "refresh":
            element.drop_tree()
            continue

        for attribute in list(element.attrib):
            name = attribute.lower()
            value = element.attrib[attribute]
            if name.startswith("on") or (
                allowed_tags is not None and name not in _ALLOWED_ATTRIBUTES
            ):
                del element.attrib[attribute]
            elif name in {"href", "src"} and not _safe_url(value, attribute=name):
                del element.attrib[attribute]
            elif name == "style" and not _safe_style(value):
                del element.attrib[attribute]


def sanitize_html_fragment(markup: str) -> str:
    """Remove active HTML and unsafe URLs while preserving safe formatting."""
    if not markup or not markup.strip():
        return markup

    root = lxml_html.fragment_fromstring(markup, create_parent="div")
    _sanitize_tree(root, allowed_tags=_ALLOWED_TAGS, drop_tags=_DROP_TAGS)

    return "".join(
        lxml_html.tostring(child, encoding="unicode", method="html")
        for child in root
    )


def sanitize_html_document(markup: str) -> str:
    """Remove active content from a complete XHTML/HTML document.

    Unlike :func:`sanitize_html_fragment`, this keeps document wrappers and
    normal EPUB head content such as stylesheets and metadata. Unknown but
    non-active tags are retained so existing EPUB-specific formatting is not
    silently damaged.
    """
    if not markup or not markup.strip():
        return markup

    xml_prefix = ""
    xml_match = re.match(r"^(\s*<\?xml[^?]*\?>\s*)", markup, re.IGNORECASE)
    if xml_match:
        xml_prefix = xml_match.group(1)
    doctype = ""
    doctype_match = re.search(r"<!DOCTYPE[^>]*>", markup, re.IGNORECASE)
    if doctype_match:
        doctype = doctype_match.group(0) + "\n"

    try:
        parser = lxml_html.HTMLParser(
            recover=True,
            encoding="utf-8",
            no_network=True,
        )
        root = lxml_html.document_fromstring(markup.encode("utf-8"), parser=parser)
        _sanitize_tree(root, allowed_tags=None, drop_tags=_DOCUMENT_DROP_TAGS)
        serialized = lxml_html.tostring(root, encoding="unicode", method="xml")
        return f"{xml_prefix}{doctype}{serialized}"
    except Exception:
        # A malformed document should still not pass active markup through.
        return sanitize_html_fragment(markup)
