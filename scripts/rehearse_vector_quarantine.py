import hashlib
import json
import sys
import time

import pg8000.dbapi

from vector_quarantine import TABLE_COLUMNS, change_vector, records


def digest_rows(connection, table):
    identifier, text_column = TABLE_COLUMNS[table]
    cursor = connection.cursor()
    cursor.execute("SELECT " + identifier + ", " + text_column + ", CAST(body_vec AS TEXT) FROM dashboard." + table + " ORDER BY " + identifier)
    digest = hashlib.sha256()
    count = 0
    for row in cursor.fetchall():
        digest.update(json.dumps(list(row), ensure_ascii=True).encode())
        count += 1
    cursor.close()
    return count, digest.hexdigest()


def main(path):
    connection = pg8000.dbapi.connect(
        host="127.0.0.1", port=5432, user="postgres", password="isolated-vector-rehearsal-only",
        database="heliosdb", timeout=30,
    )
    connection.autocommit = True
    cursor = connection.cursor()
    cursor.execute("CREATE SCHEMA dashboard")
    for table, (identifier, text_column) in TABLE_COLUMNS.items():
        cursor.execute("CREATE TABLE dashboard." + table + " (" + identifier + " TEXT PRIMARY KEY, " + text_column + " TEXT, body_vec VECTOR(384))")
    started = time.monotonic()
    expected = {table: hashlib.sha256() for table in TABLE_COLUMNS}
    expected_counts = {table: 0 for table in TABLE_COLUMNS}
    for number, record in enumerate(records(path), 1):
        table = record["table"]
        cursor.execute("INSERT INTO dashboard." + table + " VALUES (%s,%s,CAST(%s AS VECTOR(384)))", (record["id"], record["text"], record["vector"]))
        expected[table].update(json.dumps([record["id"], record["text"], record["vector"]], ensure_ascii=True).encode())
        expected_counts[table] += 1
        if number % 1000 == 0:
            print("restore_loaded", number, flush=True)
    initial = {table: digest_rows(connection, table) for table in TABLE_COLUMNS}
    for table in TABLE_COLUMNS:
        assert initial[table] == (expected_counts[table], expected[table].hexdigest()), "Backup restore differs"
    changed = sum(change_vector(connection, record) for record in records(path))
    assert changed == sum(expected_counts.values())
    assert sum(change_vector(connection, record) for record in records(path)) == 0
    for table in TABLE_COLUMNS:
        cursor.execute("SELECT COUNT(*) FROM dashboard." + table + " WHERE body_vec IS NOT NULL")
        assert cursor.fetchone()[0] == 0
    restored = sum(change_vector(connection, record, restore=True) for record in records(path))
    assert restored == changed
    final = {table: digest_rows(connection, table) for table in TABLE_COLUMNS}
    assert final == initial, "Round-trip modified source text or vector values"
    first = next(records(path))
    table = first["table"]
    identifier, text_column = TABLE_COLUMNS[table]
    cursor.execute("UPDATE dashboard." + table + " SET " + text_column + "=%s WHERE " + identifier + "=%s", ("changed synthetic guard", first["id"]))
    assert change_vector(connection, first) == 0
    print(json.dumps({"restored_from_backup": expected_counts, "quarantined": changed, "restored": restored, "idempotence": True, "changed_source_guard": True, "roundtrip_hashes_match": True, "elapsed_seconds": round(time.monotonic() - started, 2)}), flush=True)
    connection.close()


if __name__ == "__main__":
    main(sys.argv[1])
