import math
import json
import unittest
from unittest.mock import patch

from token_dashboard import clusters


def vector(first=1.0, second=0.0):
    return [first, second] + [0.0] * 382


def vector_text(first=1.0, second=0.0):
    return "[" + ",".join(str(value) for value in vector(first, second)) + "]"


class MockCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, args=()):
        self.calls.append((sql, args))

    def fetchall(self):
        return self.rows


class MockDB:
    def __init__(self, rows):
        self.cursor_obj = MockCursor(rows)

    def cursor(self):
        return self.cursor_obj


class ClusterVectorTests(unittest.TestCase):
    def test_oversized_integer_is_rejected_without_overflow_escape(self):
        with self.assertRaises(ValueError):
            clusters._parse_vector(vector(10 ** 400))

    def rows(self, *vectors):
        return [(str(index), "prompt %d" % index, value) for index, value in enumerate(vectors)]

    def test_both_select_branches_cast_body_vector_to_text(self):
        for project_slug, expected_args in ((None, ()), ("demo", ("demo",))):
            with self.subTest(project_slug=project_slug):
                db = MockDB(self.rows(vector_text(), vector_text(0.0, 1.0)))
                with patch.object(clusters.hw, "get_conn", return_value=db), \
                     patch.object(clusters.hw, "_commit"):
                    result = clusters.cluster_prompts(k=1, project_slug=project_slug)
                select_sql, args = db.cursor_obj.calls[0]
                self.assertIn("CAST(body_vec AS TEXT)", select_sql)
                self.assertEqual(args, expected_args)
                self.assertTrue(result["ok"])

    def test_parse_vector_preserves_fractional_coordinates(self):
        parsed = clusters._parse_vector(vector_text(0.123456789, 0.25))
        self.assertEqual(parsed[0], 0.123456789)
        self.assertEqual(parsed[1], 0.25)

    def test_parse_vector_rejects_invalid_values(self):
        invalid = [
            vector_text(math.nan),
            vector_text(math.inf),
            vector_text(1e100),
            vector_text(1e-50),
            vector_text(True),
            vector_text(0.0, 0.0),
            "[1,2]",
            "[not-a-number," + ",".join(["0"] * 383) + "]",
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate[:24]):
                with self.assertRaises(ValueError):
                    clusters._parse_vector(candidate)

    def test_parse_vector_rejects_list_booleans_and_numeric_strings(self):
        for candidate in ([True] + [0.0] * 383, ["1.0"] + [0.0] * 383):
            with self.subTest(candidate_type=type(candidate[0])):
                with self.assertRaises(ValueError):
                    clusters._parse_vector(candidate)

    def test_parse_vector_rejects_json_quoted_numeric_coordinates(self):
        candidate = json.dumps(["1.0"] + [0.0] * 383)
        with self.assertRaises(ValueError):
            clusters._parse_vector(candidate)

    def test_invalid_row_does_not_delete_or_insert(self):
        invalid = [
            vector_text(True),
            vector_text(math.nan),
            vector_text(math.inf),
            vector_text(1e100),
            vector_text(1e-50),
            vector_text(0.0, 0.0),
            "[1,2]",
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate[:24]):
                db = MockDB([
                    ("secret-valid-id", "prompt", vector_text()),
                    ("secret-invalid-id", "prompt", candidate),
                ])
                with patch.object(clusters.hw, "get_conn", return_value=db):
                    result = clusters.cluster_prompts(k=1)
                self.assertFalse(result["ok"])
                self.assertIn("invalid vector", result["error"])
                self.assertNotIn("secret-invalid-id", result["error"])
                self.assertFalse(any("DELETE" in sql or "INSERT" in sql for sql, _ in db.cursor_obj.calls))

    def test_valid_two_distinct_vectors_cluster_with_k_one(self):
        db = MockDB(self.rows(vector_text(), vector_text(0.0, 1.0)))
        with patch.object(clusters.hw, "get_conn", return_value=db), \
             patch.object(clusters.hw, "_commit"):
            result = clusters.cluster_prompts(k=1, max_iter=10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["n_prompts"], 2)
        self.assertEqual(result["labels"][0]["size"], 2)
        member_inserts = [args for sql, args in db.cursor_obj.calls if "INSERT INTO dashboard.cluster_members" in sql]
        self.assertEqual(len(member_inserts), 2)
        self.assertTrue(all(args[2] > 0 for args in member_inserts))


if __name__ == "__main__":
    unittest.main()
