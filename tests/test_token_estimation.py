"""トークン見積もり(estimate_text_tokens/estimate_tokens)の単体テスト。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402


class TestEstimateTextTokens(unittest.TestCase):
    def test_ascii_is_four_chars_per_token(self):
        self.assertEqual(s.estimate_text_tokens("a" * 40), 10)

    def test_non_ascii_is_one_char_per_token(self):
        self.assertEqual(s.estimate_text_tokens("あ" * 10), 10)

    def test_mixed_ascii_and_non_ascii(self):
        # ascii 8文字(2トークン) + 非ascii 3文字(3トークン)
        self.assertEqual(s.estimate_text_tokens("abcdefgh" + "あいう"), 5)

    def test_empty_string(self):
        self.assertEqual(s.estimate_text_tokens(""), 0)


class TestEstimateTokens(unittest.TestCase):
    def test_message_overhead_only(self):
        msgs = [{"role": "user", "content": ""}]
        self.assertEqual(s.estimate_tokens(msgs), 8)

    def test_multiple_messages_sum_overhead(self):
        msgs = [{"role": "user", "content": ""}, {"role": "assistant", "content": ""}]
        self.assertEqual(s.estimate_tokens(msgs), 16)

    def test_tool_calls_increase_estimate(self):
        with_tc = [{"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.c"}}}]}]
        without_tc = [{"role": "assistant", "content": ""}]
        self.assertGreater(s.estimate_tokens(with_tc), s.estimate_tokens(without_tc))

    def test_images_add_fixed_cost_per_image(self):
        msgs = [{"role": "user", "content": "", "images": ["b64a", "b64b"]}]
        self.assertEqual(s.estimate_tokens(msgs), 8 + 2 * s.TOKENS_PER_IMAGE)


if __name__ == "__main__":
    unittest.main()
