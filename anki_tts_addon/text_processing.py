"""
Text processing utilities for Anki TTS.
Extracts speakable text from Anki card HTML.
"""

import re
import html as html_module


# Greek letters and math symbols -> spoken forms
SYMBOL_REPLACEMENTS = {
    "\u03c0": "pi",
    "\u03b1": "alpha",
    "\u03b2": "beta",
    "\u03b3": "gamma",
    "\u03b4": "delta",
    "\u03b5": "epsilon",
    "\u03b8": "theta",
    "\u03bb": "lambda",
    "\u03bc": "mu",
    "\u03c3": "sigma",
    "\u03c4": "tau",
    "\u03c6": "phi",
    "\u03c9": "omega",
    "\u00b1": "plus or minus",
    "\u2192": "arrow",
    "\u221e": "infinity",
    "\u2248": "approximately",
    "\u2260": "not equal",
    "\u2264": "less than or equal to",
    "\u2265": "greater than or equal to",
    "\u2211": "sum",
    "\u220f": "product",
    "\u222b": "integral",
    "\u0394": "delta",
    "\u2207": "nabla",
}

_SYMBOL_PATTERN = re.compile(
    "|".join(map(re.escape, SYMBOL_REPLACEMENTS.keys()))
)

SPOKEN_CLOZE_PLACEHOLDER = "bla bla bla"
RENDERED_CLOZE_PLACEHOLDER_PATTERN = re.compile(
    r"\[\s*(?:\.\.\.|\u2026)\s*\]", re.IGNORECASE
)
RAW_CLOZE_UNWRAP_PATTERN = re.compile(
    r"\{\{c\d+::(.*?)(?:::.*?)?\}\}", re.DOTALL | re.IGNORECASE
)
RAW_CLOZE_CAPTURE_PATTERN = re.compile(
    r"\{\{c(\d+)::(.*?)(?:::.*?)?\}\}", re.DOTALL | re.IGNORECASE
)
_TAG_TOKEN_PATTERN = re.compile(
    r"<(/?)([a-zA-Z0-9]+)([^>]*?)(/?)>", re.DOTALL
)
CLASS_ATTR_PATTERN = re.compile(
    r"\bclass\s*=\s*([\"'])(.*?)\1", re.DOTALL | re.IGNORECASE
)


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = html_module.unescape(text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _replace_symbols(text: str) -> str:
    """Replace Greek letters and math symbols with spoken forms."""
    return _SYMBOL_PATTERN.sub(
        lambda m: SYMBOL_REPLACEMENTS[m.group()], text
    )


MAX_SPEAKABLE_LENGTH = 500


def _strip_math(text: str) -> str:
    """Strip MathJax/LaTeX delimiters and their contents from text."""
    # \( ... \)  inline MathJax
    text = re.sub(r"\\\(.*?\\\)", "", text, flags=re.DOTALL)
    # \[ ... \]  display MathJax
    text = re.sub(r"\\\[.*?\\\]", "", text, flags=re.DOTALL)
    # $$ ... $$  display LaTeX (strip before single $ to avoid partial match)
    text = re.sub(r"\$\$.*?\$\$", "", text, flags=re.DOTALL)
    # $ ... $  inline LaTeX — only match if content has LaTeX commands (\)
    # This avoids stripping dollar amounts like "$5.00"
    text = re.sub(r"\$(?=[^$]*\\)[^$]+\$", "", text)
    return text


def _mask_active_raw_cloze(content: str, active_ord: int) -> str:
    """Mask only the active raw cloze; keep inactive cloze text visible."""
    if active_ord is None:
        # Safe fallback when we don't know the active card ordinal.
        return RAW_CLOZE_CAPTURE_PATTERN.sub(SPOKEN_CLOZE_PLACEHOLDER, content)

    active_cloze_num = active_ord + 1

    def _replace(match: re.Match) -> str:
        cloze_num = int(match.group(1))
        cloze_text = match.group(2)
        if cloze_num == active_cloze_num:
            return SPOKEN_CLOZE_PLACEHOLDER
        return cloze_text

    return RAW_CLOZE_CAPTURE_PATTERN.sub(_replace, content)


def _is_rendered_active_cloze_tag(attrs: str) -> bool:
    class_match = CLASS_ATTR_PATTERN.search(attrs)
    if not class_match:
        return False
    classes = set(class_match.group(2).split())
    # Anki marks the active (hidden) cloze with class "cloze" and inactive
    # (visible) clozes with "cloze-inactive". Some note types/add-ons apply
    # BOTH tokens to inactive clozes, so an element is only treated as active
    # when it carries "cloze" and not "cloze-inactive".
    return "cloze" in classes and "cloze-inactive" not in classes


def _find_active_cloze_spans(content: str, mask: bool):
    """Detect, and optionally mask, active ``class="cloze"`` elements.

    Returns ``(content, found_active_cloze)`` in a single pass. When ``mask``
    is False the content is returned unchanged and only detection happens;
    when True, each active cloze element's whole body is replaced with the
    spoken placeholder. The nesting depth of the cloze element's own tag name
    is tracked so a cloze wrapped in another element, or containing nested
    same-name children, is masked in full instead of leaking part of the
    answer.

    Callers pass ``mask=False`` when a rendered ``[...]`` placeholder is
    present: in that case the active cloze is already masked by the placeholder
    substitution and the remaining ``class="cloze"`` spans are inactive,
    visible text that must be preserved (older Anki and some note types wrap
    inactive clozes in ``class="cloze"`` too).
    """
    out = []
    last = 0
    active_tag = None
    active_depth = 0
    found = False

    for match in _TAG_TOKEN_PATTERN.finditer(content):
        between = content[last:match.start()]
        last = match.end()
        is_close = match.group(1) == "/"
        tag = match.group(2).lower()
        attrs = match.group(3)
        self_close = match.group(4) == "/"

        if active_tag is None:
            out.append(between)
            if not is_close and _is_rendered_active_cloze_tag(attrs):
                found = True
                if mask:
                    out.append(SPOKEN_CLOZE_PLACEHOLDER)
                    if not self_close:
                        active_tag = tag
                        active_depth = 1
                else:
                    out.append(match.group(0))
            else:
                out.append(match.group(0))
        elif not self_close and tag == active_tag:
            # Inside an active cloze: drop content, balance same-name tags.
            if is_close:
                active_depth -= 1
                if active_depth == 0:
                    active_tag = None
            else:
                active_depth += 1

    if active_tag is None:
        out.append(content[last:])
    return "".join(out), found


def extract_speakable_text(
    html_str: str,
    strip_question: bool = False,
    active_ord: int = None,
) -> str:
    """
    Extract speakable text from Anki card HTML.

    Args:
        html_str: Raw HTML from card.question() or card.answer()
        strip_question: If True, strip the question portion from an answer.
            Answer HTML includes question + <hr id=answer> + answer content.
        active_ord: 0-indexed card ordinal (card.ord). Kept for API
            compatibility; cloze masking is now based on rendered/raw content.
    """
    if not html_str:
        return ""

    content = html_str

    # If processing answer, strip everything before the answer separator
    if strip_question:
        match = re.search(
            r'<hr\s+id\s*=\s*["\']?answer["\']?\s*/?\s*>',
            content,
            re.IGNORECASE,
        )
        if match:
            content = content[match.end():]

    # Try to extract from a #text div (common card template pattern)
    text_match = re.search(
        r'<div[^>]*id="text"[^>]*>(.*?)</div>', content, re.DOTALL
    )
    if text_match:
        content = text_match.group(1)

    # Image-only cards: if content is only <img> tags, return empty
    img_only = re.sub(r"<img[^>]*>", "", content)
    img_only = re.sub(r"<[^>]+>", "", img_only).strip()
    if not img_only and re.search(r"<img[^>]*>", content):
        return ""

    # A rendered "[...]" means Anki has already hidden the active cloze; the
    # remaining class="cloze" spans are then inactive, visible text.
    had_rendered_placeholder = bool(
        RENDERED_CLOZE_PLACEHOLDER_PATTERN.search(content)
    )

    # Replace [...] cloze placeholders with spoken form
    content = RENDERED_CLOZE_PLACEHOLDER_PATTERN.sub(
        SPOKEN_CLOZE_PLACEHOLDER, content
    )

    if strip_question:
        # On answer side, preserve answers and only unwrap raw cloze syntax.
        content = RAW_CLOZE_UNWRAP_PATTERN.sub(r"\1", content)
    else:
        # Single pass over class="cloze" spans. When the active cloze was shown
        # as [...] (already masked above) we only DETECT spans and leave their
        # visible inactive text intact; otherwise (templates that hide the
        # active cloze via CSS while keeping its text) we mask the span itself.
        content, has_rendered_cloze = _find_active_cloze_spans(
            content, mask=not had_rendered_placeholder
        )
        if has_rendered_cloze:
            # Rendered cloze markers reflect what Anki is actually hiding; any
            # remaining raw markers in the same HTML are visible text.
            content = RAW_CLOZE_UNWRAP_PATTERN.sub(r"\1", content)
        else:
            # No rendered cloze: mask only the active raw cloze by ordinal.
            content = _mask_active_raw_cloze(content, active_ord)

    # Strip MathJax/LaTeX before HTML removal (delimiters may span tags)
    content = _strip_math(content)

    # Strip non-content elements
    content = re.sub(r"<script.*?</script>", "", content, flags=re.DOTALL)
    content = re.sub(r"<style.*?</style>", "", content, flags=re.DOTALL)
    content = re.sub(
        r'<div class="timer".*?</div>', "", content, flags=re.DOTALL
    )
    content = re.sub(
        r'<div id="tags-container".*?</div>', "", content, flags=re.DOTALL
    )

    # Clean to plain text
    clean = _strip_html(content)
    clean = _replace_symbols(clean)
    clean = re.sub(r"\s+", " ", clean)
    clean = clean.strip()

    # Cap length to avoid excessively long readings
    if len(clean) > MAX_SPEAKABLE_LENGTH:
        clean = clean[:MAX_SPEAKABLE_LENGTH].rsplit(" ", 1)[0] + "..."

    return clean
