import argparse
import ast
import collections
import hashlib
import json
import math
from pathlib import Path
import struct

from legacy_embedding_match import classify


def load_legacy_function(path):
    source = Path(path).read_bytes()
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_hash_embedding")
    namespace = {"hashlib": hashlib, "math": math, "struct": struct, "EMBED_DIM": 384}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_hash_embedding"], hashlib.sha256(source).hexdigest()


def audit(input_path, legacy_path):
    legacy_function, legacy_sha = load_legacy_function(legacy_path)
    counts = collections.defaultdict(collections.Counter)
    seen = collections.defaultdict(set)
    summaries = {}
    input_sha = hashlib.sha256()
    header_seen = False
    footer_seen = False
    with Path(input_path).open("rb") as source:
        for line in source:
            input_sha.update(line)
            record = json.loads(line)
            kind = record["kind"]
            if footer_seen:
                raise ValueError("Records found after export footer")
            if kind == "header":
                if header_seen or counts:
                    raise ValueError("Duplicate or misplaced header")
                header_seen = True
            elif kind == "row":
                if not header_seen:
                    raise ValueError("Missing export header")
                table = record["table"]
                identifier = record["id"]
                if table not in {"messages", "tool_results"} or identifier in seen[table]:
                    raise ValueError("Unexpected table or duplicate identifier")
                seen[table].add(identifier)
                counts[table][classify(record["text"], record["vector"], legacy_function)] += 1
            elif kind == "table_summary":
                table = record["table"]
                if table in summaries:
                    raise ValueError("Duplicate table summary")
                summaries[table] = record
            elif kind == "footer":
                footer_seen = record.get("complete") is True
            else:
                raise ValueError("Unknown export record")
    if not header_seen or not footer_seen or set(summaries) != {"messages", "tool_results"}:
        raise ValueError("Incomplete export")
    coverage = all(
        summary["before"] == summary["exported"] == summary["after"] == sum(counts[table].values())
        for table, summary in summaries.items()
    )
    return {
        "input_sha256": input_sha.hexdigest(), "legacy_source_sha256": legacy_sha,
        "stable_count_coverage": coverage, "tables": dict(counts), "summaries": summaries,
        "scope": "Known legacy function comparison; not provider provenance or semantic-quality certification",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("legacy_source")
    args = parser.parse_args()
    print(json.dumps(audit(args.input, args.legacy_source), indent=2))


if __name__ == "__main__":
    main()
