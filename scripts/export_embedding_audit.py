import json
import sys
import time

from token_dashboard import helios_writer


MAX_ROWS_PER_TABLE = 25000
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


def main():
    connection = helios_writer._connect_raw()
    output_bytes = 0

    def emit(record):
        nonlocal output_bytes
        line = json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n"
        output_bytes += len(line.encode())
        if output_bytes > MAX_OUTPUT_BYTES:
            raise RuntimeError("Audit output limit exceeded; partial export is not complete")
        sys.stdout.write(line)

    try:
        emit({"kind": "header", "started_at": time.time(), "purpose": "read-only bounded vector audit"})
        for table, identifier, text_column in [
            ("messages", "uuid", "prompt_text"),
            ("tool_results", "tool_use_id", "body_text"),
        ]:
            cursor = connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM dashboard." + table + " WHERE body_vec IS NOT NULL")
            before = cursor.fetchone()[0]
            if before > MAX_ROWS_PER_TABLE:
                raise RuntimeError("Audit row limit exceeded")
            cursor.execute(
                "SELECT " + identifier + ", " + text_column + ", CAST(body_vec AS TEXT) FROM dashboard." + table
                + " WHERE body_vec IS NOT NULL ORDER BY " + identifier + " LIMIT " + str(MAX_ROWS_PER_TABLE + 1)
            )
            rows = cursor.fetchall()
            if len(rows) > MAX_ROWS_PER_TABLE:
                raise RuntimeError("Audit row limit exceeded")
            for identifier_value, text, vector in rows:
                emit({"kind": "row", "table": table, "id": identifier_value, "text": text, "vector": vector})
            cursor.execute("SELECT COUNT(*) FROM dashboard." + table + " WHERE body_vec IS NOT NULL")
            after = cursor.fetchone()[0]
            emit({"kind": "table_summary", "table": table, "before": before, "exported": len(rows), "after": after})
            cursor.close()
        emit({"kind": "footer", "finished_at": time.time(), "complete": True})
    finally:
        connection.close()


if __name__ == "__main__":
    main()
