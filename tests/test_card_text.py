import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module


class FakeCard:
    ord = 0

    def __init__(self, rendered_question=None, question_html="raw question"):
        self.rendered_question = rendered_question
        self.question_html = question_html
        self.render_reload_values = []

    def render_output(self, reload=False):
        self.render_reload_values.append(reload)
        return types.SimpleNamespace(question_text=self.rendered_question)

    def question(self):
        return self.question_html

    def answer(self):
        return f"{self.question_html}<hr id=answer>raw answer"


class FailingRenderCard(FakeCard):
    def render_output(self, reload=False):
        self.render_reload_values.append(reload)
        raise RuntimeError("render failed")


class NoFallbackCallsCard:
    ord = 0

    def render_output(self, reload=False):
        raise AssertionError("render_output should not be called")

    def question(self):
        raise AssertionError("question should not be called")


class CardTextTests(unittest.TestCase):
    def setUp(self):
        self.card_text = load_addon_module("card_text")

    def test_rendered_question_html_prefers_render_output_question_text(self):
        card = FakeCard(
            rendered_question="<div>rendered question</div>",
            question_html="<div>raw question</div>",
        )

        html = self.card_text.rendered_question_html(card)

        self.assertEqual(html, "<div>rendered question</div>")
        self.assertEqual(card.render_reload_values, [True])

    def test_rendered_question_html_prefers_empty_fallback_html(self):
        card = NoFallbackCallsCard()

        html = self.card_text.rendered_question_html(card, "")

        self.assertEqual(html, "")

    def test_rendered_question_html_falls_back_to_question_when_render_fails(self):
        card = FailingRenderCard(question_html="<div>fallback question</div>")

        with self.assertLogs(self.card_text.log, level="WARNING") as logs:
            html = self.card_text.rendered_question_html(card)

        self.assertEqual(html, "<div>fallback question</div>")
        self.assertEqual(card.render_reload_values, [True])
        self.assertIn(
            "Card render_output() failed during TTS text extraction",
            logs.output[0],
        )

    def test_speakable_question_text_uses_rendered_cloze_html(self):
        card = FakeCard()
        rendered_html = (
            'Ruxolitinib has JAK2, myelofibrosis, '
            '<span class="cloze">[...]</span>'
        )

        text = self.card_text.speakable_question_text(card, rendered_html)

        self.assertEqual(
            text,
            "Ruxolitinib has JAK2, myelofibrosis, bla bla bla",
        )

    def test_speakable_answer_text_strips_question_side(self):
        card = FakeCard()
        rendered_html = (
            "Ruxolitinib has <span class=\"cloze\">[...]</span>"
            "<hr id=answer>"
            "myelofibrosis"
        )

        text = self.card_text.speakable_answer_text(card, rendered_html)

        self.assertEqual(text, "myelofibrosis")


if __name__ == "__main__":
    unittest.main()
