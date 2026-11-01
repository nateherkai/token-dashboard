import json
import math
import unittest
from unittest.mock import Mock, patch

from token_dashboard import embedder


def vector(value=1.0):
    return [value] + [0.0] * (embedder.EMBED_DIM - 1)


class EmbedderTests(unittest.TestCase):
    def setUp(self):
        embedder._cache.clear()

    def tearDown(self):
        embedder._cache.clear()

    def configured(self, responses):
        return patch.multiple(
            embedder.mcp_client,
            is_configured=lambda: True,
            http_url=lambda: "http://mcp.test",
            call_tool=responses,
        )

    def test_unavailable_then_recovery_same_text(self):
        responses = Mock(side_effect=[RuntimeError("down"), {"content": [{"embedding": vector(2.0)}]}])
        with self.configured(responses):
            self.assertIsNone(embedder.embed("same"))
            self.assertEqual(embedder.embed("same"), vector(2.0))
        self.assertEqual(responses.call_count, 2)

    def test_cache_is_defensive_and_success_only(self):
        responses = Mock(return_value={"content": [{"embedding": vector(3.0)}]})
        with self.configured(responses):
            result = embedder.embed("cached")
            result[0] = 99.0
            self.assertEqual(embedder.embed("cached")[0], 3.0)
        self.assertEqual(responses.call_count, 1)

    def test_malformed_results_and_error_flag(self):
        malformed = [
            None,
            [],
            {"isError": True, "content": [{"embedding": vector()}]},
            {"content": None},
            {"content": [None, [], {"text": "{"}, {"text": json.dumps({"embedding": "bad"})}]},
        ]
        for result in malformed:
            with self.subTest(result=result):
                with self.configured(Mock(return_value=result)):
                    self.assertIsNone(embedder.embed(repr(result)))

    def test_valid_structured_and_json_content(self):
        result = {"structuredContent": {"embedding": vector(7.0)}}
        with self.configured(Mock(return_value=result)):
            self.assertEqual(embedder.embed("structuredContent"), vector(7.0))
        structured = {"content": [{"embedding": vector(4.0)}]}
        with self.configured(Mock(return_value=structured)):
            self.assertEqual(embedder.embed("structured"), vector(4.0))

        encoded = {"content": [{"type": "text", "text": json.dumps({"vector": vector(5.0)})}]}
        with self.configured(Mock(return_value=encoded)):
            self.assertEqual(embedder.embed("json"), vector(5.0))

    def test_rejects_empty_zero_wrong_dimension_nonnumeric_and_nonfinite(self):
        invalid = [
            [],
            [0.0] * embedder.EMBED_DIM,
            vector()[:-1],
            ["1"] + [0.0] * (embedder.EMBED_DIM - 1),
            [True] + [0.0] * (embedder.EMBED_DIM - 1),
            [math.nan] + [0.0] * (embedder.EMBED_DIM - 1),
            [math.inf] + [0.0] * (embedder.EMBED_DIM - 1),
            vector(1e100),
            vector(1e-10),
        ]
        for candidate in invalid:
            with self.subTest(candidate_type=type(candidate)):
                result = {"content": [{"embedding": candidate}]}
                with self.configured(Mock(return_value=result)):
                    self.assertIsNone(embedder.embed(repr(candidate)))

    def test_batch_and_availability_use_validation(self):
        responses = Mock(side_effect=[
            {"content": [{"embedding": vector(6.0)}]},
            {"content": [{"embedding": [0.0] * embedder.EMBED_DIM}]},
        ])
        with self.configured(responses):
            self.assertEqual(embedder.embed_batch(["ok", "bad"]), [vector(6.0), None])

        with patch.object(embedder.mcp_client, "is_configured", return_value=False):
            self.assertFalse(embedder.is_real_embedding_available())

    def test_cache_capacity_is_bounded(self):
        with patch.object(embedder, "_cache_max", 2), \
             self.configured(Mock(return_value={"embedding": vector()})):
            for number in range(10):
                self.assertIsNotNone(embedder.embed("text-%d" % number))
                self.assertLessEqual(len(embedder._cache), 2)


if __name__ == "__main__":
    unittest.main()
