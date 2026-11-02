"""Offline structural reconciliation for vector-quarantine evidence."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


MAX_INPUT_BYTES = 256 * 1024 * 1024
TABLES = ("messages", "tool_results")
OUTCOME_STAGES = {"applied", "skipped_changed_row"}


def _unique_object(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("Duplicate JSON field")
        record[key] = value
    return record


def _invalid_constant(value):
    raise ValueError("Invalid JSON constant")


def _read_jsonl(path):
    try:
        with Path(path).open("rb") as source:
            payload = source.read(MAX_INPUT_BYTES + 1)
    except OSError as error:
        raise ValueError("Unable to read evidence file") from error
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("Evidence file exceeds byte bound")
    records = []
    for line in payload.splitlines():
        if not line.strip():
            raise ValueError("Blank evidence record")
        try:
            record = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError) as error:
            raise ValueError("Malformed JSONL evidence") from error
        if not isinstance(record, dict):
            raise ValueError("Evidence record must be an object")
        records.append(record)
    if not records:
        raise ValueError("Evidence file is empty")
    return payload, records


def _identity(record):
    table = record.get("table")
    identifier = record.get("id")
    if table not in TABLES or not isinstance(identifier, str) or not identifier:
        raise ValueError("Invalid journal identity")
    return table, identifier


def _validate_snapshot(path):
    payload, records = _read_jsonl(path)
    digest = hashlib.sha256(payload).hexdigest()
    if records[0].get("kind") != "header":
        raise ValueError("Snapshot header must be first")

    identities = []
    seen = set()
    summaries = {}
    footer_seen = False
    table_index = 0
    for record in records:
        kind = record.get("kind")
        if footer_seen:
            raise ValueError("Records found after snapshot footer")
        if kind == "header":
            if record is not records[0]:
                raise ValueError("Misplaced snapshot header")
            if record.get("purpose") != "read-only bounded vector audit":
                raise ValueError("Unsupported snapshot format")
        elif kind == "row":
            if table_index >= len(TABLES) or record.get("table") != TABLES[table_index]:
                raise ValueError("Snapshot row is out of order")
            if not all(isinstance(record.get(key), str) for key in ("id", "text", "vector")) or not record["vector"]:
                raise ValueError("Invalid snapshot row")
            identity = _identity(record)
            if identity in seen:
                raise ValueError("Duplicate snapshot identity")
            seen.add(identity)
            identities.append(identity)
        elif kind == "table_summary":
            table = record.get("table")
            if table_index >= len(TABLES) or table != TABLES[table_index]:
                raise ValueError("Invalid snapshot summary")
            counts = [record.get(key) for key in ("before", "exported", "after")]
            if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
                raise ValueError("Invalid snapshot summary counts")
            if not counts[0] == counts[1] == counts[2]:
                raise ValueError("Snapshot summary counts disagree")
            summaries[table] = counts[0]
            table_index += 1
        elif kind == "footer":
            if table_index != len(TABLES) or record.get("complete") is not True:
                raise ValueError("Incomplete snapshot footer")
            footer_seen = True
        else:
            raise ValueError("Unexpected snapshot record")

    if not footer_seen or set(summaries) != set(TABLES):
        raise ValueError("Incomplete snapshot")
    for table in TABLES:
        if summaries[table] != sum(identity[0] == table for identity in identities):
            raise ValueError("Snapshot row counts do not match summaries")
    return digest, tuple(identities)


def _validate_journal(path, snapshot_digest, snapshot_identities, pilot=False):
    _, records = _read_jsonl(path)
    header = records[0]
    if header.get("stage") != "header":
        raise ValueError("Journal header must be first")
    if header.get("snapshot_sha256") != snapshot_digest:
        raise ValueError("Journal snapshot hash mismatch")
    planned = header.get("planned")
    if isinstance(planned, bool) or not isinstance(planned, int) or planned < 0:
        raise ValueError("Invalid journal planned count")
    if planned != len(snapshot_identities):
        raise ValueError("Journal planned count does not match snapshot")

    expected_identities = snapshot_identities[:planned]
    intents = []
    outcomes = []
    intent_set = set()
    outcome_set = set()
    footer = None
    for record in records[1:]:
        stage = record.get("stage")
        if footer is not None:
            raise ValueError("Records found after journal footer")
        if stage == "intent":
            if len(intents) != len(outcomes):
                raise ValueError("Journal contains pending intents")
            identity = _identity(record)
            if identity in intent_set or len(intents) >= planned:
                raise ValueError("Duplicate or excess journal intent")
            if identity != expected_identities[len(intents)]:
                raise ValueError("Journal intent order does not match snapshot")
            intent_set.add(identity)
            intents.append(identity)
        elif stage in OUTCOME_STAGES:
            identity = _identity(record)
            if len(intents) != len(outcomes) + 1 or identity != intents[-1] or identity in outcome_set:
                raise ValueError("Unpaired journal outcome")
            outcome_set.add(identity)
            outcomes.append((identity, stage))
        elif stage in {"complete", "batch_complete"}:
            footer = record
        elif stage == "halted":
            raise ValueError("Journal halted before verification")
        else:
            raise ValueError("Unexpected journal stage")

    pending = len(intent_set - outcome_set)
    if pending:
        raise ValueError("Journal contains pending intents")
    if len(outcome_set) != len(intents):
        raise ValueError("Journal intent and outcome counts disagree")

    if footer is None:
        raise ValueError("Journal is incomplete")
    if footer["stage"] == "batch_complete":
        if not pilot or len(outcomes) >= planned:
            raise ValueError("Unexpected journal batch footer")
    elif len(outcomes) != planned:
        raise ValueError("Complete journal is incomplete")

    applied = sum(stage == "applied" for _, stage in outcomes)
    skipped = sum(stage == "skipped_changed_row" for _, stage in outcomes)
    footer_counts = (footer.get("changed", 0), footer.get("skipped", 0))
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in footer_counts):
        raise ValueError("Invalid journal footer counts")
    if footer_counts != (applied, skipped):
        raise ValueError("Journal footer counts disagree")
    return {
        "planned": planned,
        "processed": len(outcomes),
        "applied": applied,
        "skipped": skipped,
        "pending": 0,
        "identities": frozenset(intent_set),
    }


def reconcile(snapshot, full_journal, pilot_journal=None):
    """Validate snapshot/journal evidence and return privacy-preserving counts."""
    digest, identities = _validate_snapshot(snapshot)
    full = _validate_journal(full_journal, digest, identities)
    result = {
        "snapshot_sha256": digest,
        "planned": len(identities),
        "processed": full["processed"],
        "applied": full["applied"],
        "skipped": full["skipped"],
        "pending": full["pending"],
        "pilot_processed": 0,
        "pilot_applied": 0,
        "pilot_skipped": 0,
        "pilot_pending": 0,
        "pilot_overlap_full": 0,
    }
    if pilot_journal is not None:
        pilot = _validate_journal(pilot_journal, digest, identities, pilot=True)
        result.update({
            "pilot_processed": pilot["processed"],
            "pilot_applied": pilot["applied"],
            "pilot_skipped": pilot["skipped"],
            "pilot_pending": pilot["pending"],
        })
        result["pilot_overlap_full"] = len(pilot["identities"] & full["identities"])
    return result


def main():
    parser = argparse.ArgumentParser(description="Reconcile vector-quarantine JSONL evidence offline")
    parser.add_argument("snapshot")
    parser.add_argument("full_journal")
    parser.add_argument("--pilot-journal")
    args = parser.parse_args()
    try:
        result = reconcile(args.snapshot, args.full_journal, args.pilot_journal)
    except Exception:
        print("Journal verification failed; evidence is invalid or incomplete.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
