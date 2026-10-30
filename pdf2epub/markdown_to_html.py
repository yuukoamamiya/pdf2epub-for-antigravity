#!/usr/bin/env python3
"""
Utility to convert markdown files to HTML with proper EPUB formatting.
Uses python-markdown library with extensions for footnotes, tables, and other features.
"""

import html as html_module
import re
import secrets
from html.parser import HTMLParser
from typing import Optional
from loguru import logger
from .utils.logging_config import configure_logging
from .utils.html_safety import sanitize_html_fragment
from .footnote_markdown import collect_footnote_definition_blocks

# We'll use markdown library - need to add to pyproject.toml: 
# poetry add markdown

# Configure logger
logger = configure_logging()

# Footnote normalization may suffix repeated local keys (for example
# ``[^1-2]``). Keep the key grammar in sync with the scanner and splitter.
FOOTNOTE_KEY_PATTERN = r"[\w-]+"
FOOTNOTE_SYNTAX_RE = re.compile(
    rf'\[\^({FOOTNOTE_KEY_PATTERN})\](?!:)|^\[\^({FOOTNOTE_KEY_PATTERN})\]:',
    re.MULTILINE,
)
ID_ATTRIBUTE_RE = re.compile(r'(\bid=")([^"]*)(")')
FRAGMENT_HREF_RE = re.compile(r'(\bhref="[^"]*#)([^"]+)(")')
TABLE_RE = re.compile(r'(<table(?:\s[^>]*)?>.*?</table>)', re.DOTALL)
TABLE_ROW_RE = re.compile(r'<tr(?:\s[^>]*)?>(.*?)</tr>', re.DOTALL)
TABLE_CELL_RE = re.compile(r'<t[hd](?:\s[^>]*)?>', re.IGNORECASE)
WIDE_TABLE_COLUMN_THRESHOLD = 8
FENCED_CODE_RE = re.compile(
    r"(?ms)^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?"
    r"^ {0,3}(?P=fence)[ \t]*(?=\n|\Z)"
)
INLINE_CODE_RE = re.compile(
    r"(?s)(?<!`)(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)"
)


def contains_footnote_syntax(markdown_content: str) -> bool:
    """Return whether markdown contains supported footnote syntax."""
    return bool(FOOTNOTE_SYNTAX_RE.search(markdown_content))


def get_epub_css():
    """Get the CSS stylesheet for EPUB HTML files."""
    return """@namespace h "http://www.w3.org/1999/xhtml";
body {
    font-family: "Hiragino Mincho ProN", "MS Mincho", serif;
    line-height: 1.8;
    max-width: 800px;
    margin: 2em auto;
    padding: 0 1em;
}
ruby {
    ruby-align: center;
}
rt {
    font-size: 0.5em;
    font-weight: normal;
}
h1 {
    text-align: center;
    margin-top: 1em;
    margin-bottom: 2em;
    font-weight: bold;
    font-size: 2em;
    border-bottom: 2px solid;
    padding-bottom: 0.5em;
}
h2 {
    font-size: 1.5em;
    font-weight: bold;
    margin-top: 2.5em;
    margin-bottom: 1em;
    border-bottom: 1px solid;
    padding-bottom: 0.3em;
}
h3 {
    font-size: 1.2em;
    font-weight: bold;
    margin-top: 2em;
    margin-bottom: 0.8em;
}
p {
    margin-bottom: 1.2em;
    text-indent: 1em;
    text-align: justify;
}
blockquote {
    margin: 1.5em 2em;
    padding-left: 1em;
    border-left: 3px solid;
    font-style: italic;
}
ul, ol {
    margin: 1em 0;
    padding-left: 2em;
}
ol {
    list-style: none;
}
ol li {
    position: relative;
}
.epub-list-marker {
    position: absolute;
    left: -2em;
    font-weight: 600;
    font-size: 0.9em;
}
.footnote-def {
    margin: 0.8em 0 0;
    font-size: 0.9em;
    line-height: 1.55;
}
.footnote-def p {
    margin: 0 0 0.65em;
    text-indent: 0;
    text-align: left;
}
li {
    margin-bottom: 0.5em;
}
code {
    padding: 0.2em 0.4em;
    border-radius: 3px;
    font-family: monospace;
}
pre {
    padding: 1em;
    border-radius: 5px;
    overflow-x: auto;
}
pre code {
    padding: 0;
}
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1.5em auto;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 1.5em 0;
}
.table-scroll {
    max-width: 100%;
    overflow-x: auto;
    margin: 1.5em 0;
}
.table-scroll table {
    width: 100%;
    margin: 0;
}
.table-scroll--wide table {
    width: auto;
    min-width: 100%;
}
.table-scroll--wide th,
.table-scroll--wide td {
    white-space: nowrap;
}
th, td {
    border: 1px solid;
    padding: 0.5em;
    text-align: left;
}
th {
    font-weight: bold;
}
.footnote {
    font-size: 0.9em;
    vertical-align: super;
}
.footnotes {
    margin-top: 4em;
    padding-top: 1em;
    border-top: 1px solid #ccc;
    font-size: 0.9em;
}
.footnotes h2 {
    font-size: 1.2em;
    border-bottom: none;
    margin-bottom: 1em;
}
.footnotes ol {
    padding-left: 1.5em;
    list-style: none;
}
.footnote-item {
    margin-bottom: 0.8em;
    line-height: 1.6;
}
sup {
    font-size: 0.8em;
    vertical-align: super;
}
sup a {
    text-decoration: none;
}
sup a:hover {
    text-decoration: underline;
}
hr {
    border: none;
    border-top: 1px solid #ccc;
    margin: 2em 0;
}
"""


def process_ruby_text(markdown_content: str) -> str:
    """
    Convert Japanese ruby text format (kanji(kana)) to HTML ruby tags.
    
    Examples:
    - 玄関(げんかん) -> <ruby>玄関<rt>げんかん</rt></ruby>
    - 一人(ひとり) -> <ruby>一人<rt>ひとり</rt></ruby>
    - 幼馴染(おさななじみ) -> <ruby>幼馴染<rt>おさななじみ</rt></ruby>
    - 今(いま) -> <ruby>今<rt>いま</rt></ruby>  (single kanji)
    """
    # Pattern to match kanji followed by parentheses containing only hiragana/katakana
    # This pattern handles:
    # - Multiple kanji characters
    # - Single kanji character
    # - Reading in parentheses containing only kana (hiragana or katakana)
    
    # Basic kana ranges
    hiragana_range = r'\u3040-\u309F'
    katakana_range = r'\u30A0-\u30FF'
    kanji_range = r'\u4E00-\u9FAF\u3400-\u4DBF'  # CJK Unified Ideographs
    
    # Pattern: One or more kanji followed by parentheses containing only kana
    # Using lookahead to ensure we don't match if there's already a <ruby> tag
    pattern = rf'(?<!<ruby>)(([{kanji_range}]+)\(([{hiragana_range}{katakana_range}ー]+)\))'
    
    def replace_ruby(match):
        full_match = match.group(1)
        kanji = match.group(2)
        reading = match.group(3)
        
        # Count the number of kanji characters
        kanji_count = len(kanji)
        reading_chars = len(reading)
        
        # Simple heuristic: if reading is much longer than kanji, it's probably a valid ruby
        # This helps avoid false positives
        if reading_chars > kanji_count * 5:
            # Probably not a valid ruby text, return as-is
            return full_match
        
        # EPUB 2 uses XHTML 1.1, where HTML5 ruby/rt elements are invalid.
        # Preserve both the base text and reading with styled XHTML spans.
        return f'<span class="ruby">{kanji}<span class="rt">{reading}</span></span>'
    
    # Apply the replacement
    markdown_content = re.sub(pattern, replace_ruby, markdown_content)
    
    return markdown_content


def preprocess_markdown(markdown_content: str, footnote_manager=None, source_chapter: Optional[str] = None, image_mapping: Optional[dict] = None) -> str:
    """
    Pre-process markdown to fix various issues before conversion.

    Args:
        markdown_content: The markdown text to process
        footnote_manager: Optional FootnoteManager for cross-chapter footnote handling
        source_chapter: The source chapter name for footnote linking
        image_mapping: Optional dict mapping original image names to new names
    """
    # Keep code literal while OCR/math/formatting preprocessing runs. Formula-
    # looking examples inside code must reach Python-Markdown unchanged.
    code_fragments = {}
    code_token_prefix = f"PDF2EPUBCODE{secrets.token_hex(16)}TOKEN"
    while code_token_prefix in markdown_content:
        code_token_prefix = f"PDF2EPUBCODE{secrets.token_hex(16)}TOKEN"

    def stash_code(match):
        token = f"{code_token_prefix}{len(code_fragments)}END"
        code_fragments[token] = match.group(0)
        return token

    markdown_content = FENCED_CODE_RE.sub(stash_code, markdown_content)
    markdown_content = INLINE_CODE_RE.sub(stash_code, markdown_content)

    # Math fragments are also kept outside the Markdown parser. This prevents
    # TeX asterisks and MathML markup from being interpreted as emphasis or
    # altered by table/smart-quote processing.
    math_fragments = {}
    math_token_prefix = f"PDF2EPUBMATH{secrets.token_hex(16)}TOKEN"
    while math_token_prefix in markdown_content:
        math_token_prefix = f"PDF2EPUBMATH{secrets.token_hex(16)}TOKEN"

    def stash_math(fragment: str) -> str:
        token = f"{math_token_prefix}{len(math_fragments)}END"
        math_fragments[token] = fragment
        return token

    # First, apply image mapping to markdown images if provided
    if image_mapping:
        for original_name, new_name in image_mapping.items():
            # Handle various image reference patterns in markdown
            markdown_content = markdown_content.replace(f']({original_name})', f']({new_name})')
            markdown_content = markdown_content.replace(f'](../{original_name})', f'](../{new_name})')
            markdown_content = markdown_content.replace(f'](../images/{original_name})', f'](../images/{new_name})')
            markdown_content = markdown_content.replace(f'](images/{original_name})', f'](images/{new_name})')

    # Then, convert markdown images inside HTML tags (like <figure>) to HTML img tags
    markdown_content = re.sub(
        r'!\[([^\]]*)\]\(([^\)]+)\)',
        r'<img alt="\1" src="\2" />',
        markdown_content
    )

    # Then, process Japanese ruby text
    markdown_content = process_ruby_text(markdown_content)
    
    # Then fix LaTeX-style table footnote markers like ${e}$ 
    # Convert them to superscript letters
    latex_markers = {
        '${a}$': '<sup>a</sup>',
        '${b}$': '<sup>b</sup>',
        '${c}$': '<sup>c</sup>',
        '${d}$': '<sup>d</sup>',
        '${e}$': '<sup>e</sup>',
        '${f}$': '<sup>f</sup>',
        '${g}$': '<sup>g</sup>',
        '${h}$': '<sup>h</sup>',
        '${i}$': '<sup>i</sup>',
        '${j}$': '<sup>j</sup>',
    }
    
    for latex, html in latex_markers.items():
        markdown_content = markdown_content.replace(latex, html)
    
    # Handle LaTeX math expressions with superscripts
    # Convert $number^{letter}$ or $number^{\mathrm{letter}}$ to number<sup>letter</sup>
    # Updated pattern to handle decimals, commas, negative numbers, and both upper/lowercase
    markdown_content = re.sub(r'\$([-0-9,\.]+)\^{\\mathrm{([a-zA-Z])}}\$', r'\1<sup>\2</sup>', markdown_content)
    markdown_content = re.sub(r'\$([-0-9,\.]+)\^{([a-zA-Z])}\$', r'\1<sup>\2</sup>', markdown_content)
    
    # Handle underlined text like $\underline{8}$ or $\underline{text}$
    markdown_content = re.sub(r'\$\\underline\{([^}]+)\}\$', r'<u>\1</u>', markdown_content)
    
    # Handle numbers with plus/minus signs like $200+$ or $50-$
    markdown_content = re.sub(r'\$([\d,\.]+)([\+\-])\$', r'\1\2', markdown_content)
    
    # Handle numbers with values in parentheses like $255(36)$
    markdown_content = re.sub(r'\$(\d+)\((\d+)\)\$', r'\1(\2)', markdown_content)
    
    # Handle negative numbers in parentheses like $(-17)$
    markdown_content = re.sub(r'\$\((-?\d+)\)\$', r'(\1)', markdown_content)
    
    # Handle years in parentheses like $(1983)$
    markdown_content = re.sub(r'\$\((\d{4})\)\$', r'(\1)', markdown_content)
    
    # Protect a literal currency sign at the start of inline math without
    # consuming the opening delimiter. For example, ``$\$1.1 \\times 10^9$``
    # keeps the currency sign while still processing the expression.
    escaped_dollar_placeholder = "PDF2EPUBESCAPEDDOLLARTOKEN"

    def protect_currency_math(match):
        math_body = match.group(1)
        return f"{escaped_dollar_placeholder}${math_body}$"

    markdown_content = re.sub(
        re.escape("$\\$") + r"([^$\n]*)" + re.escape("$"),
        protect_currency_math,
        markdown_content,
    )

    # Handle standalone escaped dollar signs: \$ -> placeholder
    # IMPORTANT: Use placeholder to avoid interfering with $...$ LaTeX block matching
    markdown_content = markdown_content.replace(r'\$', escaped_dollar_placeholder)

    # OCR backends may emit inline formulas as raw HTML ``<math>...</math>``
    # instead of Markdown ``$...$``. Markdown preserves those HTML nodes as-is,
    # so convert text-only math nodes here before the Markdown renderer runs.
    # Nodes that already contain MathML children are intentionally left alone.
    from latex2mathml import converter

    def process_embedded_math(match):
        body = match.group('body')
        if re.search(r'<\s*/?\s*[A-Za-z]', body):
            return match.group(0)

        latex_code = html_module.unescape(body).strip()
        if not latex_code:
            return match.group(0)

        try:
            return stash_math(converter.convert(latex_code))
        except Exception as e:
            logger.warning(
                f"Failed to convert embedded math: {latex_code[:50]}... Error: {e}"
            )
            return match.group(0)

    markdown_content = re.sub(
        r'<math\b[^>]*>(?P<body>.*?)</math\s*>',
        process_embedded_math,
        markdown_content,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # OCR/refine placeholders such as "$$$" mean an unknown page number, not math.
    # Protect them before the $$...$$ display-math pass so they cannot consume
    # following paragraphs or footnote definitions.
    dollar_run_placeholders = {}

    def protect_dollar_run(match):
        token = f"<<<DOLLAR_RUN_{len(dollar_run_placeholders)}>>>"
        dollar_run_placeholders[token] = match.group(0)
        return token

    markdown_content = re.sub(r'\${3,}', protect_dollar_run, markdown_content)

    # === DISPLAY MATH: Convert $$...$$ blocks to MathML ===
    # Process display math BEFORE inline math to avoid conflicts
    def process_display_math(match):
        """
        Convert display math $$...$$ to MathML using latex2mathml.

        Display math is rendered as block-level centered mathematical expressions.
        """
        latex_code = match.group(1).strip()

        if not latex_code:
            # Empty display math block, return placeholder
            return '<div class="math-display"></div>'

        try:
            # Convert LaTeX to MathML
            mathml = converter.convert(latex_code)

            # Wrap in a centered div for display math
            return stash_math(
                f'<div class="math-display" style="text-align: center; margin: 1em 0;">{mathml}</div>'
            )
        except Exception as e:
            logger.warning(f"Failed to convert display math to MathML: {latex_code[:50]}... Error: {e}")
            # Fallback: keep original LaTeX in a styled div
            return stash_math(
                f'<div class="math-display" style="text-align: center; margin: 1em 0; font-style: italic;">$${latex_code}$$</div>'
            )

    # Match $$...$$ blocks (multiline, non-greedy)
    # Use DOTALL flag to allow matching across newlines
    markdown_content = re.sub(r'\$\$\s*\n?(.*?)\n?\s*\$\$', process_display_math, markdown_content, flags=re.DOTALL)

    # === INLINE MATH: Use unicodeitplus for LaTeX to Unicode conversion ===
    # Fallback to MathML for complex expressions
    from unicodeitplus import replace as latex_to_unicode

    def process_latex_block(match):
        r"""
        Process a single $...$ LaTeX block with Unicode-first, MathML-fallback strategy.

        Strategy:
        1. Try preprocessing for known patterns
        2. Try unicodeitplus for Unicode conversion
        3. If conversion incomplete (still has LaTeX commands), use MathML
        4. If MathML fails, keep original LaTeX

        Preprocessing:
        - \qquad → double space (unicodeitplus doesn't handle this)
        - ^{\prime} → \prime (for prime symbol conversion)
        - \underline{X} → <u>X</u> (for HTML underline instead of Unicode combining)
        - \operatorname{X} → X (unicodeitplus doesn't handle this well)

        Unicodeitplus handles:
        - Greek letters (\alpha, \Delta, etc.)
        - Superscripts and subscripts (x^{2}, x_{i})
        - Math symbols (\cdot, \cdots, \ldots, \quad, \%)
        - Text commands (\mathrm{X}, \text{X})
        - Bar notation (\bar{x})

        MathML fallback for:
        - Fractions (\frac{a}{b})
        - Square roots (\sqrt{x})
        - Complex expressions that unicodeitplus can't handle
        """
        content = match.group(1)  # Get content between $ and $
        original_content = content  # Keep original for fallback

        # Plain Latin variables in economic circulation formulas do not need
        # mathematical italic Unicode code points, which are missing in many
        # ebook fonts. Keep the original letters and use ordinary italic text.
        math_variable = r"[A-Za-z](?:(?:['′])|(?:\^\{?\\prime\}?))?"
        if re.fullmatch(rf"\s*{math_variable}(?:\s*[-=+]\s*{math_variable})*\s*", content):
            safe_expression = re.sub(r"\^\{?\\prime\}?", "′", content.strip())
            return stash_math(f'<i>{html_module.escape(safe_expression)}</i>')

        # === Preprocessing for patterns unicodeitplus doesn't handle well ===

        # 1. \qquad → double space (unicodeitplus leaves it unchanged)
        content = content.replace(r'\qquad', '  ')

        # 2. Convert ^{\prime} or ^{\prime X} to \prime X (unicodeitplus converts \prime but not ^{\prime})
        # Also handle cases like { }^{\prime 1} where prime is in superscript with other content
        content = re.sub(r'\^\{\\prime\s*([^}]*)\}', r'\\prime\1', content)
        content = re.sub(r'\^\\prime\b', r'\\prime', content)

        # 3. \underline{X} -> <u>X</u> (for HTML underline instead of Unicode combining characters)
        # Do this BEFORE unicodeitplus to avoid it converting to Unicode combining underline
        content = re.sub(r'\\underline\{([^}]+)\}', r'<u>\1</u>', content)

        # 4. \operatorname{X} -> X (unicodeitplus doesn't handle this well)
        content = re.sub(r'\\operatorname\{([^}]+)\}', r'\1', content)

        # === Try unicodeitplus for standard LaTeX to Unicode conversion ===
        try:
            result = latex_to_unicode(content)

            # Check if conversion was complete
            # If result still contains backslash commands (except in HTML tags like <u>),
            # it means unicodeitplus couldn't convert everything
            # Remove HTML tags temporarily for checking
            check_result = re.sub(r'<[^>]+>', '', result)

            if '\\' in check_result:
                # Conversion incomplete, fallback to MathML
                logger.debug(f"Unicode conversion incomplete for: {original_content[:50]}... Trying MathML")
                raise ValueError("Incomplete conversion, use MathML")

            return stash_math(result)

        except Exception as e:
            # === Fallback to MathML for complex expressions ===
            try:
                mathml = converter.convert(original_content)
                logger.debug(f"Using MathML for inline math: {original_content[:50]}...")
                return stash_math(f'<span class="math-inline">{mathml}</span>')
            except Exception as e2:
                # Last resort: keep original LaTeX with styling
                logger.warning(f"Failed to convert inline math: {original_content[:50]}... Error: {e2}")
                return stash_math(
                    f'<span style="font-style: italic;">${original_content}$</span>'
                )

    # Apply the callback to all $...$ blocks (inline only, not across newlines)
    # Use [^$\n]+ to match only within a single line
    markdown_content = re.sub(r'\$([^$\n]+)\$', process_latex_block, markdown_content)

    # Now that formulas are protected by tokens, handle ordinary Markdown
    # emphasis without pairing TeX constructs such as Q^* or align*.
    markdown_content = re.sub(
        r'(?<!\*)\*(?!\*)([^\*\n]+?)(?<!\*)\*(?!\*)',
        r'<em>\1</em>',
        markdown_content,
    )

    # Convert placeholders back to actual dollar signs
    markdown_content = markdown_content.replace(escaped_dollar_placeholder, '$')
    for token, dollar_run in dollar_run_placeholders.items():
        markdown_content = markdown_content.replace(token, dollar_run)

    # Now process footnotes
    markdown_content = preprocess_footnotes(
        markdown_content, footnote_manager, source_chapter
    )
    for token, fragment in math_fragments.items():
        markdown_content = markdown_content.replace(token, fragment)
    for token, code in code_fragments.items():
        markdown_content = markdown_content.replace(token, code)
    return markdown_content


def preprocess_footnotes_local(markdown_content: str, footnote_manager, source_chapter: str) -> str:
    """
    Process footnotes in LOCAL mode with multi-part chapter support.

    Handles footnotes within a chapter, including cross-part references.

    Args:
        markdown_content: The markdown text to process
        footnote_manager: FootnoteManager instance in LOCAL mode
        source_chapter: The source chapter name for footnote linking

    Returns:
        Processed markdown with HTML footnote links
    """
    lines = markdown_content.split('\n')
    processed_lines = []
    definition_occurrence_in_file = {}
    reference_occurrence_in_file = {}

    definitions, consumed = collect_footnote_definition_blocks(
        lines,
        allow_no_colon=footnote_manager.is_no_colon_definition_chapter(source_chapter),
    )
    parsed_lines = [
        (index + 1, line, definitions.get(index))
        for index, line in enumerate(lines)
        if index not in consumed
    ]
    for line_num, line, definition in parsed_lines:
        # Check for footnote definition [^key]:
        def_match = definition
        if def_match:
            fn_key, fn_text = def_match
            definition_occurrence_in_file[fn_key] = definition_occurrence_in_file.get(fn_key, 0) + 1
            processed_lines.append(
                footnote_manager.get_definition_html(
                    fn_key,
                    fn_text,
                    source_chapter,
                    line_num=line_num,
                    occurrence_in_file=definition_occurrence_in_file[fn_key],
                )
            )
        else:
            # Process footnote references [^key]
            def replace_ref(match):
                fn_key = match.group(1)
                page_note_match = re.match(r"^(\d+)n(\d+)$", fn_key)
                counter_key = page_note_match.group(2) if page_note_match else fn_key
                reference_occurrence_in_file[counter_key] = reference_occurrence_in_file.get(counter_key, 0) + 1
                return footnote_manager.get_footnote_html(
                    fn_key,
                    source_chapter,
                    line_num=line_num,
                    occurrence_in_file=reference_occurrence_in_file[counter_key],
                ) or match.group(0)

            # Replace footnote references
            line = re.sub(
                rf'\[\^({FOOTNOTE_KEY_PATTERN})\](?!:)',
                replace_ref,
                line,
            )

            # Replace backticks with proper apostrophes/quotes
            # This prevents markdown from interpreting them as code blocks
            # Single backticks are typically used as quotes in this context
            line = line.replace("`", "'")
            processed_lines.append(line)

    return '\n'.join(processed_lines)


def preprocess_footnotes_global(markdown_content: str, footnote_manager, source_chapter: str) -> str:
    """
    Process footnotes in GLOBAL mode - handles cross-chapter footnote references.
    
    For definition chapters: Keep footnote definitions as-is in markdown format
    For reference chapters: Convert footnote references to cross-chapter links
    
    Args:
        markdown_content: The markdown text to process
        footnote_manager: FootnoteManager instance in GLOBAL mode
        source_chapter: The source chapter name for footnote linking
        
    Returns:
        Processed markdown with HTML footnote links
    """
    lines = markdown_content.split('\n')
    processed_lines = []
    reference_occurrence_in_file = {}
    definition_occurrence_in_file = {}
    is_definition_chapter = footnote_manager.is_definition_chapter(source_chapter)

    definitions, consumed = collect_footnote_definition_blocks(
        lines,
        allow_no_colon=(
            is_definition_chapter
            and footnote_manager.is_no_colon_definition_chapter(source_chapter)
        ),
    )
    parsed_lines = [
        (index + 1, line, definitions.get(index))
        for index, line in enumerate(lines)
        if index not in consumed
    ]
    for line_num, line, definition in parsed_lines:
        def_match = definition
        if def_match and is_definition_chapter:
            fn_key, fn_text = def_match
            definition_occurrence_in_file[fn_key] = definition_occurrence_in_file.get(fn_key, 0) + 1
            processed_lines.append(
                footnote_manager.get_definition_html(
                    fn_key,
                    fn_text,
                    source_chapter,
                    line_num=line_num,
                    occurrence_in_file=definition_occurrence_in_file[fn_key],
                )
            )
            continue

        def replace_ref(match):
            fn_key = match.group(1)
            page_note_match = re.match(r"^(\d+)n(\d+)$", fn_key)
            counter_key = page_note_match.group(2) if page_note_match else fn_key
            reference_occurrence_in_file[counter_key] = reference_occurrence_in_file.get(counter_key, 0) + 1
            return footnote_manager.get_footnote_html(
                fn_key,
                source_chapter,
                line_num=line_num,
                occurrence_in_file=reference_occurrence_in_file[counter_key],
            ) or match.group(0)

        line = re.sub(
            rf'\[\^({FOOTNOTE_KEY_PATTERN})\](?!:)',
            replace_ref,
            line,
        )
        processed_lines.append(line)

    return '\n'.join(processed_lines)


def preprocess_footnotes(markdown_content: str, footnote_manager=None, source_chapter: Optional[str] = None) -> str:
    """
    Convert footnote definitions to HTML format and handle footnote references.

    Since we're not using the markdown footnotes extension, we need to manually
    convert [^1]: text to proper HTML footnote format.

    Footnote conversion is manager-backed only. This keeps local, split-part, and
    global notes on the same mapping/backref implementation.

    Args:
        markdown_content: The markdown text to process
        footnote_manager: FootnoteManager for local/global footnote handling
        source_chapter: The source chapter name for footnote linking
    """
    if not contains_footnote_syntax(markdown_content):
        return markdown_content

    if not footnote_manager or not source_chapter:
        raise ValueError("Footnote markdown requires FootnoteManager and source_chapter")

    from pdf2epub.epub.footnotes import FootnoteStyle
    if footnote_manager.get_style() == FootnoteStyle.GLOBAL:
        return preprocess_footnotes_global(markdown_content, footnote_manager, source_chapter)
    if footnote_manager.get_style() == FootnoteStyle.LOCAL:
        return preprocess_footnotes_local(markdown_content, footnote_manager, source_chapter)

    raise ValueError(f"Unsupported footnote style: {footnote_manager.get_style()}")


def convert_markdown_to_html(
    markdown_content: str,
    title: Optional[str] = None,
    include_css: bool = True,
    standalone: bool = True,
    image_mapping: Optional[dict] = None,
    footnote_manager=None,
    source_chapter: Optional[str] = None,
    preserve_soft_line_breaks: bool = False,
) -> str:
    """
    Convert markdown content to HTML.
    
    Args:
        markdown_content: The markdown text to convert
        title: Optional title for the HTML document
        include_css: Whether to include CSS styles
        standalone: Whether to create a complete HTML document
        image_mapping: Optional dict mapping original image names to new names
        footnote_manager: Optional FootnoteManager for cross-chapter footnote handling
        source_chapter: The source chapter name (e.g., "chapter_1") for footnote linking
        preserve_soft_line_breaks: Render single Markdown newlines as XHTML ``<br />``.
            The default follows standard Markdown semantics: single newlines are soft
            wraps and only blank lines create paragraph breaks. Explicit ``<br />``
            tags remain effective regardless of this option.
    
    Returns:
        HTML string
    """
    try:
        import markdown
    except ImportError:
        logger.error("markdown library not installed. Run: poetry add markdown")
        raise
    
    # Pre-process markdown to fix various issues (including image mapping)
    markdown_content = preprocess_markdown(markdown_content, footnote_manager, source_chapter, image_mapping)
    
    # Configure markdown extensions
    extensions = [
        # Remove footnotes extension - we'll handle them manually
        'markdown.extensions.tables',       # For table support
        'markdown.extensions.fenced_code',  # For code blocks
        'markdown.extensions.codehilite',   # For code syntax highlighting
        'markdown.extensions.sane_lists',   # Better list handling
        'markdown.extensions.smarty',       # Smart quotes and dashes
        'markdown.extensions.toc',          # Table of contents
        'markdown.extensions.meta',         # Metadata support
    ]
    if preserve_soft_line_breaks:
        extensions.insert(4, 'markdown.extensions.nl2br')
    
    # Configure extension settings
    extension_configs = {
        'markdown.extensions.codehilite': {
            'css_class': 'highlight',
            'linenums': False,
        },
        'markdown.extensions.toc': {
            'baselevel': 1,
            'permalink': False,
        },
    }
    
    # Create markdown instance with XHTML output for EPUB compatibility
    md = markdown.Markdown(
        extensions=extensions,
        extension_configs=extension_configs,
        output_format='xhtml'  # EPUB requires XHTML, not HTML5
    )
    
    # Convert markdown to HTML
    html_body = md.convert(markdown_content)

    # Post-process HTML
    html_body = post_process_html(html_body)
    # Markdown and model output are untrusted document content. Preserve
    # normal book formatting while removing active elements and unsafe URLs.
    html_body = sanitize_html_fragment(html_body)

    if not standalone:
        return html_body
    
    # Create full XHTML document for EPUB
    css = get_epub_css() if include_css else ""
    
    # EPUB requires XHTML 1.1 with proper DOCTYPE
    safe_title = html_module.escape(title or 'Chapter', quote=True)
    html = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja">
<head>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
    <title>{safe_title}</title>
    {f'<style type="text/css">{css}</style>' if css else '<link rel="stylesheet" type="text/css" href="../stylesheet.css"/>'}
</head>
<body>
{html_body}
</body>
</html>"""
    
    return html


def _normalize_epub_xml_id(value: str) -> str:
    """Return an EPUB 2/XHTML 1.1-compatible XML Name."""
    normalized = "".join(
        char if (char.isalnum() or char in "_.-") else "-"
        for char in value
    )
    if not normalized:
        return "epub-id"
    if not (normalized[0].isalpha() or normalized[0] == "_"):
        normalized = f"epub-id-{normalized}"
    return normalized


def _normalize_epub_ids(html: str) -> str:
    """Normalize IDs and same-document fragment links deterministically."""
    id_map = {}

    def replace_id(match: re.Match) -> str:
        original = match.group(2)
        normalized = _normalize_epub_xml_id(original)
        id_map[original] = normalized
        return f'{match.group(1)}{normalized}{match.group(3)}'

    html = ID_ATTRIBUTE_RE.sub(replace_id, html)

    def replace_fragment(match: re.Match) -> str:
        original = match.group(2)
        normalized = id_map.get(original, original)
        return f'{match.group(1)}{normalized}{match.group(3)}'

    return FRAGMENT_HREF_RE.sub(replace_fragment, html)


def _normalize_ordered_list_starts(html: str) -> str:
    """Materialize ordered-list markers without XHTML-invalid ``start`` attrs.

    EPUB readers differ in CSS counter support. Explicit marker spans keep
    regular, nested, and continued lists readable while preserving the XHTML
    1.1-compatible list structure and all list item content.
    """
    line_offsets = [0]
    line_offsets.extend(match.end() for match in re.finditer("\n", html))
    edits: list[tuple[int, int, str]] = []

    def absolute_offset(position: tuple[int, int]) -> int:
        line, column = position
        return line_offsets[line - 1] + column

    def attribute_spans(tag_text: str) -> list[tuple[str, int, int, int, int]]:
        """Return tag attribute names and raw/full value spans."""
        name_match = re.match(r"<\s*[^\s/>]+", tag_text)
        if not name_match:
            return []
        result = []
        index = name_match.end()
        while index < len(tag_text):
            whitespace_start = index
            while index < len(tag_text) and tag_text[index].isspace():
                index += 1
            if index >= len(tag_text) or tag_text[index] in "/>":
                break
            name_start = index
            while index < len(tag_text) and not tag_text[index].isspace() and tag_text[index] not in "=/>":
                index += 1
            name = tag_text[name_start:index].lower()
            while index < len(tag_text) and tag_text[index].isspace():
                index += 1
            value_start = value_end = index
            if index < len(tag_text) and tag_text[index] == "=":
                index += 1
                while index < len(tag_text) and tag_text[index].isspace():
                    index += 1
                if index < len(tag_text) and tag_text[index] in {'"', "'"}:
                    quote = tag_text[index]
                    value_start = index + 1
                    index = value_start
                    while index < len(tag_text) and tag_text[index] != quote:
                        index += 1
                    value_end = index
                    index = min(len(tag_text), index + 1)
                else:
                    value_start = index
                    while index < len(tag_text) and not tag_text[index].isspace() and tag_text[index] not in "/>":
                        index += 1
                    value_end = index
            result.append((name, whitespace_start, index, value_start, value_end))
        return result

    def rewrite_ol_tag(raw: str, attrs: list[tuple[str, str | None]], start: int) -> str:
        spans = attribute_spans(raw)
        start_attr = next((item for item in spans if item[0] == "start"), None)
        updated = raw
        if start_attr:
            _name, attr_start, attr_end, _value_start, _value_end = start_attr
            updated = updated[:attr_start] + updated[attr_end:]
        if start == 1:
            return updated

        classes = next((value or "" for name, value in attrs if name.lower() == "class"), "")
        class_tokens = classes.split()
        if "epub-continued-list" not in class_tokens:
            class_tokens.append("epub-continued-list")
        class_value = " ".join(class_tokens)
        class_attr = next((item for item in attribute_spans(updated) if item[0] == "class"), None)
        if class_attr:
            _name, attr_start, attr_end, _value_start, _value_end = class_attr
            return updated[:attr_start] + f' class="{html_module.escape(class_value, quote=True)}"' + updated[attr_end:]
        closing = updated.rfind(">")
        return updated[:closing] + f' class="{html_module.escape(class_value, quote=True)}"' + updated[closing:]

    class OrderedListMarkerParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.lists: list[tuple[str, int]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            tag_start = absolute_offset(self.getpos())
            raw = self.get_starttag_text()
            if tag == "ol":
                start_value = next((value for name, value in attrs if name.lower() == "start"), None)
                try:
                    start = int(start_value) if start_value is not None else 1
                except ValueError:
                    start = 1
                if start_value is not None:
                    rewritten = rewrite_ol_tag(raw, attrs, start)
                    if rewritten != raw:
                        edits.append((tag_start, tag_start + len(raw), rewritten))
                self.lists.append(("ol", start - 1))
                return

            if tag == "ul":
                self.lists.append(("ul", 0))
            elif tag == "li" and self.lists and self.lists[-1][0] == "ol":
                list_type, number = self.lists[-1]
                number += 1
                self.lists[-1] = (list_type, number)
                offset = tag_start + len(raw)
                edits.append((offset, offset, f'<span class="epub-list-marker">{number:02d}.</span>'))

        def handle_endtag(self, tag: str) -> None:
            if tag in {"ol", "ul"} and self.lists and self.lists[-1][0] == tag:
                self.lists.pop()

    parser = OrderedListMarkerParser()
    parser.feed(html)
    parser.close()
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        html = html[:start] + replacement + html[end:]
    return html


def _wrap_tables_for_scrolling(html: str) -> str:
    """Wrap tables and keep only genuinely wide tables from forcing no-wrap."""

    def replace_table(match: re.Match) -> str:
        table = match.group(1)
        column_count = max(
            (
                len(TABLE_CELL_RE.findall(row))
                for row in TABLE_ROW_RE.findall(table)
            ),
            default=0,
        )
        classes = "table-scroll"
        if column_count >= WIDE_TABLE_COLUMN_THRESHOLD:
            classes += " table-scroll--wide"
        return f'<div class="{classes}">{table}</div>'

    return TABLE_RE.sub(replace_table, html)


def post_process_html(html: str) -> str:
    """
    Post-process the HTML to ensure EPUB XHTML 1.1 compatibility.
    """
    # Fix image paths if they're relative and don't already have ../images/
    html = re.sub(
        r'<img ([^>]*?)src="(?!http)(?!\.\./)([^"]+)"',
        r'<img \1src="../images/\2"',
        html
    )
    
    # Ensure footnote references have proper IDs
    html = re.sub(
        r'<sup id="fnref:(\d+)">',
        r'<sup id="fnref\1">',
        html
    )
    
    # Fix footnote backlinks
    html = re.sub(
        r'href="#fnref:(\d+)"',
        r'href="#fnref\1"',
        html
    )

    html = _normalize_epub_ids(html)
    html = _normalize_ordered_list_starts(html)
    html = _wrap_tables_for_scrolling(html)
    
    # Ensure proper XHTML self-closing tags
    html = re.sub(r'<br(?!\s*/)>', '<br />', html)
    html = re.sub(r'<hr(?!\s*/)>', '<hr />', html)
    html = re.sub(r'<img ([^>]+?)(?<!/)>', r'<img \1 />', html)
    html = re.sub(r'<meta ([^>]+?)(?<!/)>', r'<meta \1 />', html)
    html = re.sub(r'<link ([^>]+?)(?<!/)>', r'<link \1 />', html)
    html = re.sub(r'<input ([^>]+?)(?<!/)>', r'<input \1 />', html)
    
    # Convert & to &amp; in text (but not in existing entities)
    html = re.sub(r'&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#[0-9]+|#x[0-9a-fA-F]+);)', '&amp;', html)

    # Convert named HTML entities (such as &rsquo;, &ldquo;, &hellip;) to valid Unicode
    # while preserving XML predefined entities (&amp;, &lt;, &gt;, &quot;, &apos;).
    xml_predefined = {"amp", "lt", "gt", "quot", "apos"}

    def replace_named_entity(match: re.Match) -> str:
        entity = match.group(1)
        if entity in xml_predefined:
            return match.group(0)
        return html_module.unescape(match.group(0))

    html = re.sub(r"&([a-zA-Z]+);", replace_named_entity, html)
    
    # Convert common block elements to have proper XHTML structure
    html = re.sub(r'<p>(\s*)</p>', '', html)  # Remove empty paragraphs
    
    return html
