import argparse
import collections
import hashlib
import io
import json
import os
import sys
from pathlib import Path
import time
from urllib.parse import urlparse

from token_dashboard import helios_writer
from vector_quarantine import change_vector
from legacy_embedding_match import _parse_vector


MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024


def frozen_records(payload):
    for line in io.BytesIO(payload):
        record = json.loads(line)
        if record["kind"] == "row":
            yield record


def validate_snapshot(payload, expected_counts):
    counts = collections.Counter()
    identifiers = set()
    summaries = {}
    header = footer = False
    for line in io.BytesIO(payload):
        record = json.loads(line)
        kind = record["kind"]
        if footer:
            raise ValueError("Records found after snapshot footer")
        if not header and kind != "header":
            raise ValueError("Snapshot header must be first")
        if kind == "header":
            if header or counts:
                raise ValueError("Misplaced snapshot header")
            header = True
        elif kind == "row":
            table = record["table"]
            if not header or table not in expected_counts:
                raise ValueError("Unexpected snapshot row")
            if not all(isinstance(record.get(key), str) for key in ["id", "text", "vector"]):
                raise ValueError("Invalid snapshot value types")
            identity = (table, record["id"])
            if not record["id"] or identity in identifiers or _parse_vector(record["vector"]) is None:
                raise ValueError("Duplicate identifier or invalid snapshot vector")
            identifiers.add(identity)
            counts[table] += 1
        elif kind == "table_summary":
            table = record["table"]
            if table not in expected_counts or table in summaries:
                raise ValueError("Unexpected table summary")
            summaries[table] = record
        elif kind == "footer":
            if record.get("complete") is not True:
                raise ValueError("Incomplete snapshot footer")
            footer = True
        else:
            raise ValueError("Unexpected snapshot record")
    if not header or not footer or set(summaries) != set(expected_counts):
        raise ValueError("Incomplete snapshot")
    for table, expected in expected_counts.items():
        summary = summaries[table]
        if not (counts[table] == expected == summary["before"] == summary["exported"] == summary["after"]):
            raise ValueError("Snapshot row counts do not match audit")


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("report")
    parser.add_argument("journal")
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rehearsal-report")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.limit <= 25000:
        raise ValueError("Batch limit must be between zero and25000")
    report = json.loads(Path(args.report).read_text())
    with open(args.snapshot, "rb") as source:
        payload = source.read(MAX_SNAPSHOT_BYTES + 1)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot exceeds byte bound")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != args.expected_sha256 or digest != report["input_sha256"]:
        raise ValueError("Snapshot hash mismatch")
    if report["stable_count_coverage"] is not True:
        raise ValueError("Audit did not cover stable table counts")
    expected = 0
    expected_counts = {}
    for table in ["messages", "tool_results"]:
        results = report["tables"][table]
        if set(results) != {"known_hash_match"}:
            raise ValueError("Snapshot includes rows not proven to be legacy hash vectors")
        count = results["known_hash_match"]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 25000:
            raise ValueError("Invalid audit count")
        expected_counts[table] = count
        expected += count
    if not args.apply:
        validate_snapshot(payload, expected_counts)
        print(json.dumps({"dry_run": True, "rows": expected, "snapshot_sha256": digest}))
        return
    if not args.rehearsal_report:
        raise ValueError("Completed full-data rehearsal evidence is required before mutation")
    rehearsal = json.loads(Path(args.rehearsal_report).read_text())
    if (
        rehearsal.get("snapshot_sha256") != digest
        or rehearsal.get("binary_sha256") != "f929bfe46ef35f426e705ed01267b637cd73482d7fd722387e192679814f80d4"
        or rehearsal.get("restored") != expected
        or rehearsal.get("quarantined") != expected
        or rehearsal.get("roundtrip_hashes_match") is not True
        or rehearsal.get("changed_source_guard") is not True
        or rehearsal.get("idempotence") is not True
        or rehearsal.get("changed_vector_guard") is not True
        or rehearsal.get("nonempty_restore_guard") is not True
    ):
        raise ValueError("Full-data rehearsal evidence is incomplete or does not match this snapshot")
    target = urlparse(helios_writer.dsn() or "")
    if target.scheme not in {"postgres", "postgresql"} or target.hostname != "172.17.0.1" or target.port != 5440 or target.path != "/heliosdb" or not target.password:
        raise ValueError("Configured target does not match the reviewed dashboard database")
    validate_snapshot(payload, expected_counts)
    descriptor = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    counts = collections.Counter()
    with os.fdopen(descriptor, "w", encoding="utf-8") as journal:
        backup_path = args.journal + ".snapshot.jsonl"
        backup_descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(backup_descriptor, "wb") as backup:
            backup.write(payload)
            backup.flush()
            os.fsync(backup.fileno())
            os.fchmod(backup.fileno(), 0o400)
        def record_event(event):
            journal.write(json.dumps(event, ensure_ascii=True) + "\n")
            journal.flush()
            os.fsync(journal.fileno())

        connection = helios_writer._connect_raw()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT version()")
            version_str = cursor.fetchone()[0]
            if "HeliosDB Nano" not in version_str:
                raise ValueError("Unexpected database version; no changes applied")
            cursor.close()
            record_event({"stage": "header", "snapshot_sha256": digest, "backup_path": backup_path, "planned": expected, "time": time.time()})
            for number, record in enumerate(frozen_records(payload), 1):
                if args.limit and number > args.limit:
                    break
                if number > expected:
                    raise ValueError("Snapshot contains more rows than audited")
                identity = {"table": record["table"], "id": record["id"]}
                record_event({"stage": "intent", **identity})
                changed = change_vector(connection, record)
                record_event({"stage": "applied" if changed else "skipped_changed_row", **identity})
                counts["changed" if changed else "skipped"] += 1
                if number % 500 == 0:
                    print(json.dumps({"processed": number, **counts}), flush=True)
                time.sleep(0.05)
            planned_batch = min(expected, args.limit) if args.limit else expected
            if sum(counts.values()) != planned_batch:
                raise ValueError("Snapshot contains fewer rows than audited")
            complete = planned_batch == expected
            record_event({"stage": "complete" if complete else "batch_complete", **counts, "time": time.time()})
            print(json.dumps({"complete": complete, "batch_complete": True, "remaining_unprocessed": expected - planned_batch, **counts}), flush=True)
        except Exception as error:
            record_event({"stage": "halted", "error_type": type(error).__name__, "time": time.time()})
            raise
        finally:
            connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Quarantine halted; inspect private journal. Error type: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
