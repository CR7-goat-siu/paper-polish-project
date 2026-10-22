import unittest

from utils.token_counter import (
    count_cjk_characters,
    estimate_tokens,
    split_into_chunks,
    split_sentences,
)


class TokenCounterTests(unittest.TestCase):
    def test_cjk_estimate(self):
        self.assertEqual(count_cjk_characters("深度学习"), 4)
        self.assertGreaterEqual(estimate_tokens("深度学习模型用于安全检测。"), 1)

    def test_chunking_preserves_paragraphs(self):
        paragraphs = ["This is paragraph one with some text for tokens."] * 12
        text = "\n\n".join(paragraphs)
        chunks = split_into_chunks(text, max_tokens=120)
        self.assertGreater(len(chunks), 1)
        # 切分无损：块内保留分隔符，直接拼接即可还原原文
        self.assertEqual("".join(chunks), text)

    def test_cjk_oversized_paragraph_is_split(self):
        # 中文段落无空格，历史上会整段绕过 chunk 上限；现在必须按句切分
        sentence = "本文提出一种基于注意力机制的信号处理方法并验证了其有效性。"
        text = sentence * 40
        chunks = split_into_chunks(text, max_tokens=60)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(estimate_tokens(chunk), 90)
        self.assertEqual("".join(chunks), text)

    def test_split_sentences_lossless(self):
        text = "Hello world. This is fine; right? 中文句子。另一句！End."
        parts = split_sentences(text)
        self.assertEqual("".join(parts), text)
        self.assertGreater(len(parts), 2)

    def test_invalid_limit(self):
        with self.assertRaises(ValueError):
            split_into_chunks("x", max_tokens=0)


if __name__ == "__main__":
    unittest.main()
