import importlib.util
from pathlib import Path
import unittest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "anki_tts_addon"
    / "review_html_cache.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "review_html_cache", _MODULE_PATH
)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
REVIEW_QUESTION_CONTEXT = _MODULE.REVIEW_QUESTION_CONTEXT
cache_review_html = _MODULE.cache_review_html
clear_review_html_cache = _MODULE.clear_review_html_cache
get_review_html = _MODULE.get_review_html

_TEXT_PROCESSING_PATH = (
    Path(__file__).resolve().parents[1]
    / "anki_tts_addon"
    / "text_processing.py"
)
_TEXT_SPEC = importlib.util.spec_from_file_location(
    "text_processing", _TEXT_PROCESSING_PATH
)
_TEXT_MODULE = importlib.util.module_from_spec(_TEXT_SPEC)
assert _TEXT_SPEC and _TEXT_SPEC.loader
_TEXT_SPEC.loader.exec_module(_TEXT_MODULE)
extract_speakable_text = _TEXT_MODULE.extract_speakable_text


class FakeCard:
    def __init__(self, card_id=123):
        self.id = card_id


class ReviewHtmlCacheTests(unittest.TestCase):
    def tearDown(self):
        clear_review_html_cache()

    def test_cache_filter_returns_html_unchanged(self):
        card = FakeCard()
        html = "<div>front</div>"

        result = cache_review_html(html, card, REVIEW_QUESTION_CONTEXT)

        self.assertEqual(result, html)

    def test_extract_review_text_prefers_rendered_question(self):
        card = FakeCard()
        raw_question = (
            "{{c2::Bladder exstrophy}} occurs due to failure of "
            "{{c1::caudal}} fold closure of the anterior abdominal wall"
        )
        rendered_question = (
            '<span class="cloze">[...]</span> occurs due to failure of '
            "caudal fold closure of the anterior abdominal wall"
        )
        cache_review_html(
            rendered_question, card, REVIEW_QUESTION_CONTEXT
        )

        selected_html = get_review_html(
            card,
            REVIEW_QUESTION_CONTEXT,
            raw_question,
        )
        result = extract_speakable_text(
            selected_html,
            active_ord=0,
        )

        self.assertEqual(
            result,
            (
                "bla bla bla occurs due to failure of caudal fold closure "
                "of the anterior abdominal wall"
            ),
        )

    def test_extract_review_text_matches_visible_cloze_question(self):
        card = FakeCard()
        raw_question = (
            "Ruxolitinib is used to treat chronic myeloproliferative "
            "disorders that have {{c3::JAK2}} mutations, including "
            "{{c1::myelofibrosis}} and {{c2::polycythemia vera}}"
        )
        rendered_question = (
            "Ruxolitinib is used to treat chronic myeloproliferative "
            "disorders that have JAK2 mutations, including myelofibrosis "
            'and <span class="cloze">[...]</span>'
        )
        cache_review_html(
            rendered_question, card, REVIEW_QUESTION_CONTEXT
        )

        selected_html = get_review_html(
            card,
            REVIEW_QUESTION_CONTEXT,
            raw_question,
        )
        result = extract_speakable_text(
            selected_html,
            active_ord=0,
        )

        self.assertEqual(
            result,
            (
                "Ruxolitinib is used to treat chronic myeloproliferative "
                "disorders that have JAK2 mutations, including "
                "myelofibrosis and bla bla bla"
            ),
        )


if __name__ == "__main__":
    unittest.main()
