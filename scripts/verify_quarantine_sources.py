"""Read-only post-quarantine verification against a bounded snapshot."""

import hashlib
import re

from reconcile_vector_quarantine import _read_jsonl, _validate_snapshot


MAX_BATCH_SIZE = 100
MAX_SNAPSHOT_ROWS = 50_000
MAX_RESULT_BYTES = 256 * 1024 * 1024
TABLE_COLUMNS = {
    "messages": ("uuid", "prompt_text"),
    "tool_results": ("tool_use_id", "body_text"),
}


def _snapshot_rows(snapshot_path, expected_sha256):
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Invalid expected snapshot hash")
    digest, identities = _validate_snapshot(snapshot_path)
    if digest != expected_sha256:
        raise ValueError("Snapshot hash mismatch")
    if len(identities) > MAX_SNAPSHOT_ROWS:
        raise ValueError("Snapshot row bound exceeded")
    payload, records = _read_jsonl(snapshot_path)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Snapshot changed during verification")
    rows = {
        (record["table"], record["id"]): record
        for record in records
        if record.get("kind") == "row"
    }
    if set(rows) != set(identities):
        raise ValueError("Snapshot identities do not match")
    return digest, rows


def _query_batch(cursor, table, identifiers):
    identifier_column, text_column = TABLE_COLUMNS[table]
    placeholders = ",".join("%s" for _ in identifiers)
    query = (
        "SELECT "
        + identifier_column
        + ", "
        + text_column
        + ", CAST(body_vec AS TEXT) FROM dashboard."
        + table
        + " WHERE "
        + identifier_column
        + " IN ("
        + placeholders
        + ") LIMIT "
        + str(len(identifiers) + 1)
    )
    cursor.execute(query, tuple(identifiers))
    result = cursor.fetchall()
    if not isinstance(result, (list, tuple)) or len(result) > len(identifiers):
        raise ValueError("Unexpected database result")
    return result


def verify(connection, snapshot_path, expected_sha256):
    """Verify source and vector state, returning aggregate privacy-safe counts."""
    digest, rows = _snapshot_rows(snapshot_path, expected_sha256)
    result = {
        "snapshot_sha256": digest,
        "snapshot_rows": len(rows),
        "present": 0,
        "missing": 0,
        "unchanged_source": 0,
        "changed_source": 0,
        "current_null": 0,
        "current_non_null": 0,
        "original_vector_still_present": 0,
        "query_count": 0,
    }
    cursor = None
    result_payload_bytes = 0
    try:
        cursor = connection.cursor()
        for table in TABLE_COLUMNS:
            identifiers = [identifier for row_table, identifier in rows if row_table == table]
            for start in range(0, len(identifiers), MAX_BATCH_SIZE):
                batch = identifiers[start:start + MAX_BATCH_SIZE]
                returned = _query_batch(cursor, table, batch)
                result["query_count"] += 1
                expected = set(batch)
                seen = set()
                for database_row in returned:
                    if not isinstance(database_row, (list, tuple)) or len(database_row) != 3:
                        raise ValueError("Malformed database result")
                    identifier, current_text, current_vector = database_row
                    if not isinstance(identifier, str) or identifier not in expected or identifier in seen:
                        raise ValueError("Unexpected database result")
                    if current_text is not None and not isinstance(current_text, str):
                        raise ValueError("Malformed database result")
                    if current_vector is not None and not isinstance(current_vector, str):
                        raise ValueError("Malformed database result")
                    result_payload_bytes += sum(
                        len(value.encode("utf-8"))
                        for value in (current_text, current_vector)
                        if value is not None
                    )
                    if result_payload_bytes > MAX_RESULT_BYTES:
                        raise ValueError("Database result payload bound exceeded")
                    seen.add(identifier)
                    original = rows[(table, identifier)]
                    result["present"] += 1
                    if current_text == original["text"]:
                        result["unchanged_source"] += 1
                    else:
                        result["changed_source"] += 1
                    if current_vector is None:
                        result["current_null"] += 1
                    else:
                        result["current_non_null"] += 1
                        if current_vector == original["vector"]:
                            result["original_vector_still_present"] += 1
                result["missing"] += len(expected - seen)
    finally:
        if cursor is not None:
            cursor.close()
    return result
