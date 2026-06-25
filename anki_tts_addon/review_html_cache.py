"""Cache reviewer HTML provided by Anki's card_will_show hook."""

REVIEW_QUESTION_CONTEXT = "reviewQuestion"
REVIEW_ANSWER_CONTEXT = "reviewAnswer"
_REVIEW_CONTEXTS = {REVIEW_QUESTION_CONTEXT, REVIEW_ANSWER_CONTEXT}

_review_html_by_card = {}


def _card_key(card):
    return getattr(card, "id", id(card))


def cache_review_html(html: str, card, context: str) -> str:
    """Remember rendered reviewer HTML and return it unchanged for the hook."""
    if context in _REVIEW_CONTEXTS:
        _review_html_by_card[(_card_key(card), context)] = html
    return html


def get_review_html(card, context: str, fallback_html: str) -> str:
    """Return Anki's rendered reviewer HTML when available."""
    return _review_html_by_card.get(
        (_card_key(card), context), fallback_html
    )


def clear_review_html_cache() -> None:
    _review_html_by_card.clear()
