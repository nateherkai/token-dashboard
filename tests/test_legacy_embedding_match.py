import json
import math
import struct
import unittest

from scripts.legacy_embedding_match import EMBED_DIM, classify


def vector(value=0.0):
    return [value] + [0.0] * (EMBED_DIM - 1)


class LegacyEmbeddingMatchTests(unittest.TestCase):
    def test_normalizes_and_truncates_text_before_calling_legacy_embed(self):
        received = []
        source = vector(1.25)

        def legacy_embed(text):
            received.append(text)
            return source

        text = "  " + ("x" * 8001) + "  "
        self.assertEqual(classify(text, source, legacy_embed), "known_hash_match")
        self.assertEqual(received, [("x" * 8000)])

    def test_matches_vector_after_float32_storage_roundtrip(self):
        legacy = [0.123456789] + [0.0] * (EMBED_DIM - 1)
        written = [float(f"{value:.6f}") for value in legacy]
        stored = [struct.unpack("<f", struct.pack("<f", value))[0] for value in written]

        self.assertEqual(classify("prompt", stored, lambda _: legacy), "known_hash_match")

    def test_clearly_different_vector_does_not_match(self):
        self.assertEqual(
            classify("prompt", vector(1.0), lambda _: vector(1.5)),
            "not_matching_known_hash",
        )

    def test_missing_or_non_string_text(self):
        valid = vector(1.0)
        for text in (None, 42, b"prompt"):
            with self.subTest(text=text):
                self.assertEqual(classify(text, valid, lambda _: valid), "missing_text")

    def test_invalid_vector_takes_precedence_over_missing_text(self):
        self.assertEqual(classify(None, vector(0.0), lambda _: vector(1.0)), "invalid_vector")

    def test_accepts_json_vector_and_rejects_malformed_json_or_lists(self):
        valid = vector(1.0)
        self.assertEqual(classify("prompt", json.dumps(valid), lambda _: valid), "known_hash_match")
        invalid = [
            "[",
            json.dumps({"vector": valid}),
            json.dumps(valid[:-1]),
            valid[:-1],
            [0.0] * EMBED_DIM,
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate if isinstance(candidate, str) else type(candidate)):
                self.assertEqual(
                    classify("prompt", candidate, lambda _: valid), "invalid_vector"
                )

    def test_rejects_nonfinite_bool_and_string_coordinates(self):
        invalid = [
            math.nan,
            math.inf,
            -math.inf,
            True,
            "1.0",
        ]
        for coordinate in invalid:
            with self.subTest(coordinate=coordinate):
                self.assertEqual(
                    classify("prompt", vector(coordinate), lambda _: vector(1.0)),
                    "invalid_vector",
                )

    def test_rejects_values_that_round_to_zero_after_six_decimals(self):
        self.assertEqual(
            classify("prompt", vector(0.0000004), lambda _: vector(1.0)),
            "invalid_vector",
        )

    def test_malformed_legacy_result_aborts_comparison(self):
        with self.assertRaises(ValueError):
            classify("prompt", vector(1.0), lambda text: [1.0])

    def test_reference_error_does_not_report_clean_vector(self):
        def broken_reference(text):
            raise RuntimeError("reference failure")
        with self.assertRaises(RuntimeError):
            classify("prompt", vector(1.0), broken_reference)


if __name__ == "__main__":
    unittest.main()
