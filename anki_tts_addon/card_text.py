"""Shared card text extraction for live TTS and background cache warmup."""

import logging
from typing import Optional

from .text_processing import extract_speakable_text

log = logging.getLogger(__name__)


def rendered_question_html(card, fallback_html: Optional[str] = None) -> str:
    if fallback_html is not None:
        return fallback_html
    try:
        output = card.render_output(reload=True)
        rendered = getattr(output, "question_text", None)
        if rendered:
            return rendered
    except Exception as e:
        log.warning("Card render_output() failed during TTS text extraction: %s", e)
    return card.question()


def speakable_question_text(card, rendered_html: Optional[str] = None) -> str:
    html = rendered_question_html(card, rendered_html)
    return extract_speakable_text(html, active_ord=card.ord)


def speakable_answer_text(card, rendered_html: Optional[str] = None) -> str:
    html = rendered_html if rendered_html is not None else card.answer()
    return extract_speakable_text(html, strip_question=True, active_ord=card.ord)
