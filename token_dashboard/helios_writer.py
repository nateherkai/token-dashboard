"""HeliosDB writer — parallels the SQLite cache for new analytics features.

Path-B item #6: dashboard's primary store is still SQLite for backwards
compatibility, but every scanned message + tool_call is also written to
HeliosDB so vector search (#1, #9), graph-rag explanations (#2), clustering
(#3), branch time-travel (#7), live alerts (#8), audit receipts (#10) can
build on the Postgres-shape schema that lives there.

Design rules:
- One schema, `dashboard`, lazily created on first scan.
- Idempotent inserts via `ON CONFLICT DO UPDATE` (uuid is PK on messages,
  (message_uuid, tool_use_id) is PK on tool_results).
- Failures are non-fatal: SQLite scan keeps working, HeliosDB writes get a
  best-effort retry on the next scan iteration.
- Embedding generation lives here too so the writer is one cohesive module.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional
from urllib.parse import urlparse

# Type alias used in retry helper above



HELIOS_DSN_ENV = "HELIOSDB_DSN"
EMBED_DIM = 384  # BGE-Small via fastembed (HeliosDB code-embed)


# ---------- connection ----------

_pool_lock = threading.Lock()
# Per-thread connections. The dashboard runs under ThreadingHTTPServer, so each
# request is served on its own thread and the frontend fires several /api calls
# in parallel (Promise.all). A single shared pg8000 connection is NOT
# thread-safe — concurrent use interleaves on one socket, corrupts the wire
# protocol, and the connection is dropped mid-response (browser sees a 502).
# Giving each thread its own connection makes reads safely concurrent and also
# stops the scan/mirror thread from contending with API reads.
_tlocal = threading.local()


def dsn() -> Optional[str]:
    return os.environ.get(HELIOS_DSN_ENV)


def is_configured() -> bool:
    return bool(dsn())


def _connect_raw():
    s = dsn()
    if not s:
        return None
    p = urlparse(s)
    if p.scheme not in ("postgres", "postgresql"):
        raise RuntimeError(f"unsupported DSN scheme: {p.scheme}")
    import pg8000.dbapi
    last_exc: Optional[Exception] = None
    # HeliosDB v3.26's TCP accept can briefly stall under concurrent load —
    # back off and retry instead of failing on the first TimeoutError.
    for attempt in range(5):
        try:
            conn = pg8000.dbapi.connect(
                host=p.hostname or "localhost",
                port=p.port or 5432,
                user=p.username or "postgres",
                password=p.password or "",
                database=(p.path or "/heliosdb").lstrip("/") or "heliosdb",
                timeout=15,
            )
            conn.autocommit = True
            return conn
        except Exception as e:
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))  # 0.5, 1, 2, 4, 8 seconds
    raise last_exc if last_exc else RuntimeError("connection failed")


def get_conn():
    """Return this thread's HeliosDB connection, creating it on first use.

    pg8000 connections are not thread-safe, and the server is threaded (see
    `_tlocal` note above), so each thread keeps its own connection. The
    connection is health-checked ("SELECT 1") and transparently reconnected if
    the socket has died. The server bounds HTTP workers and closes cached
    connections when each worker finishes. Scan tasks close theirs after each
    run; other callers must explicitly release their own thread's connection."""
    conn = getattr(_tlocal, "conn", None)
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            return conn
        except Exception:
            try: conn.close()
            except Exception: pass
            _tlocal.conn = None
            conn = None
    conn = _connect_raw()
    _tlocal.conn = conn
    if conn is not None:
        # Serialize the one-time schema bootstrap so concurrent first-connects
        # don't race on DDL; _ensure_schema short-circuits on _schema_ok after
        # the first thread, so steady-state contention here is nil.
        with _pool_lock:
            _ensure_schema(conn)
    return conn


# ---------- schema ----------

_schema_ok = False


def _ensure_schema(conn) -> None:
    global _schema_ok
    if _schema_ok:
        return
    cur = conn.cursor()
    # Create schema + tables. VECTOR(384) matches BGE-Small dimension.
    # Some statements are tried independently so a partial-schema state from
    # a previous run still completes.
    statements = [
        "CREATE SCHEMA IF NOT EXISTS dashboard",
        # messages — one row per assistant or user turn
        """CREATE TABLE IF NOT EXISTS dashboard.messages (
            uuid                    TEXT PRIMARY KEY,
            parent_uuid             TEXT,
            session_id              TEXT NOT NULL,
            project_slug            TEXT NOT NULL,
            cwd                     TEXT,
            type                    TEXT NOT NULL,
            timestamp               TEXT NOT NULL,
            model                   TEXT,
            message_id              TEXT,
            input_tokens            INT  NOT NULL DEFAULT 0,
            output_tokens           INT  NOT NULL DEFAULT 0,
            cache_read_tokens       INT  NOT NULL DEFAULT 0,
            cache_create_5m_tokens  INT  NOT NULL DEFAULT 0,
            cache_create_1h_tokens  INT  NOT NULL DEFAULT 0,
            prompt_text             TEXT,
            prompt_chars            INT,
            tool_calls_json         TEXT,
            body_vec                VECTOR(384)
        )""",
        # tool_calls — one row per tool_use block
        """CREATE TABLE IF NOT EXISTS dashboard.tool_calls (
            id              BIGINT PRIMARY KEY,
            message_uuid    TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            project_slug    TEXT NOT NULL,
            tool_name       TEXT NOT NULL,
            target          TEXT,
            tool_use_id     TEXT,
            result_tokens   INT,
            is_error        INT  NOT NULL DEFAULT 0,
            timestamp       TEXT NOT NULL,
            baseline_tokens INT,
            baseline_method TEXT,
            resolution      TEXT,
            mcp_meta_json   TEXT
        )""",
        # tool_results — one row per `_tool_result` block (paired by tool_use_id)
        """CREATE TABLE IF NOT EXISTS dashboard.tool_results (
            tool_use_id     TEXT PRIMARY KEY,
            message_uuid    TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            project_slug    TEXT NOT NULL,
            result_tokens   INT,
            is_error        INT  NOT NULL DEFAULT 0,
            timestamp       TEXT NOT NULL,
            body_text       TEXT,
            body_vec        VECTOR(384)
        )""",
        # files — same shape as SQLite for parity
        """CREATE TABLE IF NOT EXISTS dashboard.files (
            path        TEXT PRIMARY KEY,
            mtime       FLOAT8 NOT NULL,
            bytes_read  INT    NOT NULL,
            scanned_at  FLOAT8 NOT NULL
        )""",
        # mcp_replay receipts (#10 — audit-grade)
        """CREATE TABLE IF NOT EXISTS dashboard.mcp_replay (
            call_id          BIGINT PRIMARY KEY,
            tool_name        TEXT NOT NULL,
            replayed_at      FLOAT8 NOT NULL,
            response_tokens  INT,
            took_ms          INT,
            ok               INT  NOT NULL,
            detail           TEXT,
            audit_signature  TEXT
        )""",
        # alerts (#8 — live "currently expensive" surfaces)
        """CREATE TABLE IF NOT EXISTS dashboard.alerts (
            id              BIGINT PRIMARY KEY,
            kind            TEXT NOT NULL,
            session_id      TEXT,
            message_uuid    TEXT,
            threshold       INT,
            observed        INT,
            created_at      FLOAT8 NOT NULL,
            acknowledged_at FLOAT8
        )""",
        # docs ingest (#4)
        """CREATE TABLE IF NOT EXISTS dashboard.docs (
            id              BIGINT PRIMARY KEY,
            project_slug    TEXT NOT NULL,
            source_path     TEXT NOT NULL,
            doc_kind        TEXT NOT NULL,
            ingested_at     FLOAT8 NOT NULL,
            section_count   INT,
            chunk_count     INT,
            graph_root_uuid TEXT
        )""",
        # clusters (#3 — semantic grouping)
        """CREATE TABLE IF NOT EXISTS dashboard.clusters (
            id            BIGINT PRIMARY KEY,
            label         TEXT NOT NULL,
            project_slug  TEXT,
            centroid      VECTOR(384),
            size          INT NOT NULL,
            built_at      FLOAT8 NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS dashboard.cluster_members (
            cluster_id  BIGINT NOT NULL,
            uuid        TEXT NOT NULL,
            distance    FLOAT8,
            PRIMARY KEY (cluster_id, uuid)
        )""",
    ]
    # HeliosDB v3.26 keeps every connection in an implicit tx — we must send
    # COMMIT after each write/DDL or the change isn't visible to the next
    # SELECT. pg8000's autocommit only suppresses BEGIN; it does not COMMIT.
    for stmt in statements:
        try:
            cur.execute(stmt)
            try: cur.execute("COMMIT")
            except Exception: pass
        except Exception as e:
            try: cur.execute("ROLLBACK")
            except Exception: pass
            print(f"[helios_writer] schema warn: {str(e)[:160]}")
    _schema_ok = True


def _commit(conn) -> None:
    """Send explicit COMMIT — required after every write on HeliosDB v3.26."""
    try:
        conn.cursor().execute("COMMIT")
    except Exception:
        pass


def delete_messages(conn, uuids, chunk: int = 40) -> int:
    """Bulk-delete message rows (and their tool_calls/tool_results) by uuid.

    Mirrors SQLite's streaming-snapshot eviction onto HeliosDB. DELETE on
    v3.33 costs ~1 s per statement and scales with IN-list length (a 500-uuid
    list is ~9 s, a 13-uuid list ~1 s), so we batch in modest chunks rather
    than one statement per row or one giant IN-list. Returns rows requested."""
    uuids = [u for u in dict.fromkeys(uuids or []) if u]  # de-dup, keep order
    if conn is None or not uuids:
        return 0
    cur = conn.cursor()
    done = 0
    for i in range(0, len(uuids), chunk):
        part = uuids[i:i + chunk]
        ph = ",".join(f"${j + 1}" for j in range(len(part)))
        for tbl, col in (("dashboard.tool_calls", "message_uuid"),
                         ("dashboard.tool_results", "message_uuid"),
                         ("dashboard.messages", "uuid")):
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE {col} IN ({ph})", tuple(part))
            except Exception:
                pass
        _commit(conn)
        done += len(part)
    return done


def reconcile_orphans(conn, sqlite_path: str, chunk: int = 40) -> dict:
    """Drop HeliosDB message rows whose uuid is absent from SQLite.

    SQLite is the source of truth and is always written before the mirror, so
    any HeliosDB message uuid not in SQLite is a superseded snapshot SQLite
    evicted (or other stale row). Used to clear drift that accumulated before
    batched eviction existed, and as a periodic safety net. Returns counts."""
    import sqlite3
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    cur = conn.cursor()
    cur.execute("SELECT uuid FROM dashboard.messages")
    helios_uuids = {r[0] for r in cur.fetchall()}
    sc = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_uuids = {r[0] for r in sc.execute("SELECT uuid FROM messages")}
    sc.close()
    orphans = list(helios_uuids - sqlite_uuids)
    deleted = delete_messages(conn, orphans, chunk=chunk)
    return {"ok": True, "helios": len(helios_uuids), "sqlite": len(sqlite_uuids),
            "orphans": len(orphans), "deleted": deleted}


def _reset_conn() -> None:
    """Drop the calling thread's connection — used after fatal errors so the
    next operation reconnects instead of stalling on a dead socket."""
    conn = getattr(_tlocal, "conn", None)
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass
    _tlocal.conn = None
    # leave _schema_ok = True so we don't re-run DDL


def close_thread_connection() -> None:
    """Release the caller's cached connection when its unit of work finishes."""
    _reset_conn()


def _is_connection_dead(e: Exception) -> bool:
    """Detect errors that mean 'this connection is no longer usable'."""
    msg = str(e).lower()
    return any(k in msg for k in (
        "timed out", "timeout", "broken pipe", "connection reset",
        "connection is closed", "cannot read from",
    ))


# ---------- ingest API ----------

def upsert_message(conn, msg: dict, embedding: Optional[list] = None) -> None:
    """Idempotent upsert of one message row. Optional 384-dim embedding."""
    cur = conn.cursor()
    vec_lit = _vector_literal(embedding) if embedding else None
    sql = """
      INSERT INTO dashboard.messages (
        uuid, parent_uuid, session_id, project_slug, cwd, type, timestamp,
        model, message_id,
        input_tokens, output_tokens, cache_read_tokens,
        cache_create_5m_tokens, cache_create_1h_tokens,
        prompt_text, prompt_chars, tool_calls_json, body_vec
      ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
        CAST($18 AS VECTOR(384))
      )
      ON CONFLICT (uuid) DO UPDATE SET
        parent_uuid=EXCLUDED.parent_uuid,
        session_id=EXCLUDED.session_id,
        project_slug=EXCLUDED.project_slug,
        cwd=EXCLUDED.cwd,
        type=EXCLUDED.type,
        timestamp=EXCLUDED.timestamp,
        model=EXCLUDED.model,
        message_id=EXCLUDED.message_id,
        input_tokens=EXCLUDED.input_tokens,
        output_tokens=EXCLUDED.output_tokens,
        cache_read_tokens=EXCLUDED.cache_read_tokens,
        cache_create_5m_tokens=EXCLUDED.cache_create_5m_tokens,
        cache_create_1h_tokens=EXCLUDED.cache_create_1h_tokens,
        prompt_text=EXCLUDED.prompt_text,
        prompt_chars=EXCLUDED.prompt_chars,
        tool_calls_json=EXCLUDED.tool_calls_json,
        body_vec=EXCLUDED.body_vec
    """
    cur.execute(sql, (
        msg["uuid"], msg.get("parent_uuid"), msg["session_id"], msg["project_slug"],
        msg.get("cwd"), msg["type"], msg["timestamp"], msg.get("model"),
        msg.get("message_id"),
        msg.get("input_tokens", 0), msg.get("output_tokens", 0),
        msg.get("cache_read_tokens", 0),
        msg.get("cache_create_5m_tokens", 0), msg.get("cache_create_1h_tokens", 0),
        msg.get("prompt_text"), msg.get("prompt_chars"),
        msg.get("tool_calls_json"), vec_lit,
    ))
    _commit(conn)


def upsert_tool_result(conn, row: dict, embedding: Optional[list] = None) -> None:
    cur = conn.cursor()
    vec_lit = _vector_literal(embedding) if embedding else None
    sql = """
      INSERT INTO dashboard.tool_results (
        tool_use_id, message_uuid, session_id, project_slug,
        result_tokens, is_error, timestamp, body_text, body_vec
      ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8, CAST($9 AS VECTOR(384))
      )
      ON CONFLICT (tool_use_id) DO UPDATE SET
        result_tokens=EXCLUDED.result_tokens,
        is_error=EXCLUDED.is_error,
        timestamp=EXCLUDED.timestamp,
        body_text=EXCLUDED.body_text,
        body_vec=EXCLUDED.body_vec
    """
    cur.execute(sql, (
        row["tool_use_id"], row["message_uuid"], row["session_id"],
        row["project_slug"], row.get("result_tokens"), row.get("is_error", 0),
        row["timestamp"], row.get("body_text"), vec_lit,
    ))
    _commit(conn)


def upsert_tool_call(conn, row: dict) -> None:
    cur = conn.cursor()
    sql = """
      INSERT INTO dashboard.tool_calls (
        id, message_uuid, session_id, project_slug, tool_name, target,
        tool_use_id, result_tokens, is_error, timestamp,
        baseline_tokens, baseline_method, resolution, mcp_meta_json
      ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14
      )
      ON CONFLICT (id) DO UPDATE SET
        tool_name=EXCLUDED.tool_name,
        target=EXCLUDED.target,
        tool_use_id=EXCLUDED.tool_use_id,
        result_tokens=EXCLUDED.result_tokens,
        baseline_tokens=EXCLUDED.baseline_tokens,
        baseline_method=EXCLUDED.baseline_method,
        resolution=EXCLUDED.resolution,
        mcp_meta_json=EXCLUDED.mcp_meta_json
    """
    cur.execute(sql, (
        row["id"], row["message_uuid"], row["session_id"], row["project_slug"],
        row["tool_name"], row.get("target"), row.get("tool_use_id"),
        row.get("result_tokens"), row.get("is_error", 0), row["timestamp"],
        row.get("baseline_tokens"), row.get("baseline_method"),
        row.get("resolution"), row.get("mcp_meta_json"),
    ))
    _commit(conn)


# ---------- vector formatting ----------

def _vector_literal(vec: list) -> str:
    """Render a Python list as the textual VECTOR literal HeliosDB accepts."""
    if vec is None:
        return None
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


# ---------- read helpers (used by /api/search, /api/explain, /api/clusters, /api/pulse) ----------

def search_prompts(query_vec: list, limit: int = 20,
                   project_slug: Optional[str] = None,
                   since: Optional[str] = None) -> list:
    """Cosine top-K over dashboard.messages.body_vec."""
    conn = get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    where = ["body_vec IS NOT NULL", "type='user'"]
    args: list = [_vector_literal(query_vec)]
    if project_slug:
        where.append("project_slug = $" + str(len(args) + 1))
        args.append(project_slug)
    if since:
        where.append("timestamp >= $" + str(len(args) + 1))
        args.append(since)
    args.append(int(limit))
    sql = f"""
      SELECT uuid, session_id, project_slug, timestamp, model,
             prompt_text, body_vec <=> CAST($1 AS VECTOR(384)) AS distance
        FROM dashboard.messages
       WHERE {' AND '.join(where)}
       ORDER BY distance
       LIMIT ${len(args)}
    """
    cur.execute(sql, tuple(args))
    return [
        {"uuid": r[0], "session_id": r[1], "project_slug": r[2],
         "timestamp": r[3], "model": r[4], "prompt_text": r[5],
         "distance": float(r[6])}
        for r in cur.fetchall()
    ]


def search_tool_results(query_vec: list, limit: int = 20) -> list:
    """Cosine top-K over dashboard.tool_results.body_vec — used by #9
    duplicate-retrieval detection."""
    conn = get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    cur.execute("""
      SELECT tool_use_id, message_uuid, session_id, project_slug,
             result_tokens, body_text,
             body_vec <=> CAST($1 AS VECTOR(384)) AS distance
        FROM dashboard.tool_results
       WHERE body_vec IS NOT NULL
       ORDER BY distance
       LIMIT $2
    """, (_vector_literal(query_vec), int(limit)))
    return [
        {"tool_use_id": r[0], "message_uuid": r[1], "session_id": r[2],
         "project_slug": r[3], "result_tokens": r[4],
         "body_text": (r[5] or "")[:500], "distance": float(r[6])}
        for r in cur.fetchall()
    ]


def find_duplicate_retrievals(threshold: float = 0.05, min_size: int = 2000,
                              limit: int = 50) -> list:
    """Self-join tool_results to find pairs whose embeddings are < threshold
    apart (i.e. very similar) — Claude reading the same content twice."""
    conn = get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    cur.execute("""
      WITH big AS (
        SELECT tool_use_id, message_uuid, session_id, body_vec, body_text, result_tokens
          FROM dashboard.tool_results
         WHERE body_vec IS NOT NULL AND result_tokens >= $1
      )
      SELECT a.tool_use_id, b.tool_use_id, a.session_id, b.session_id,
             a.result_tokens, b.result_tokens,
             a.body_vec <=> b.body_vec AS distance,
             SUBSTR(a.body_text, 1, 200) AS preview
        FROM big a
        JOIN big b ON a.tool_use_id < b.tool_use_id
       WHERE a.body_vec <=> b.body_vec < $2
       ORDER BY distance ASC
       LIMIT $3
    """, (int(min_size), float(threshold), int(limit)))
    return [
        {"a_id": r[0], "b_id": r[1], "a_session": r[2], "b_session": r[3],
         "a_tokens": r[4], "b_tokens": r[5], "distance": float(r[6]),
         "preview": r[7]}
        for r in cur.fetchall()
    ]


def explain_prompt(uuid: str, neighbours: int = 5) -> dict:
    """RAG-style explain (#2): turn + parent + similar prompts + tool calls."""
    conn = get_conn()
    if conn is None:
        return {"error": "helios not configured"}
    cur = conn.cursor()
    cur.execute("""
      SELECT uuid, parent_uuid, session_id, project_slug, type, timestamp,
             model, prompt_text, prompt_chars, tool_calls_json,
             input_tokens, output_tokens, cache_read_tokens, body_vec IS NOT NULL
        FROM dashboard.messages WHERE uuid = $1
    """, (uuid,))
    row = cur.fetchone()
    if not row:
        return {"error": f"prompt {uuid} not found"}
    out = {
        "uuid": row[0], "parent_uuid": row[1], "session_id": row[2],
        "project_slug": row[3], "type": row[4], "timestamp": row[5],
        "model": row[6], "prompt_text": row[7], "prompt_chars": row[8],
        "tool_calls_json": row[9], "input_tokens": row[10],
        "output_tokens": row[11], "cache_read_tokens": row[12],
        "has_embedding": bool(row[13]),
    }
    # Sibling tool calls
    cur.execute("""
      SELECT tool_name, target, tool_use_id, result_tokens, baseline_tokens
        FROM dashboard.tool_calls
       WHERE message_uuid = $1
       ORDER BY id
    """, (uuid,))
    out["tool_calls"] = [
        {"tool_name": r[0], "target": r[1], "tool_use_id": r[2],
         "result_tokens": r[3], "baseline_tokens": r[4]}
        for r in cur.fetchall()
    ]
    # Tool results paired by tool_use_id
    cur.execute("""
      SELECT tr.tool_use_id, tr.result_tokens, SUBSTR(tr.body_text,1,400)
        FROM dashboard.tool_results tr
        JOIN dashboard.tool_calls tc ON tc.tool_use_id = tr.tool_use_id
       WHERE tc.message_uuid = $1
    """, (uuid,))
    out["tool_results"] = [
        {"tool_use_id": r[0], "result_tokens": r[1], "preview": r[2]}
        for r in cur.fetchall()
    ]
    # Similar prompts (only if this row has an embedding)
    if out["has_embedding"]:
        cur.execute("""
          SELECT m.uuid, m.session_id, m.project_slug, m.timestamp,
                 SUBSTR(m.prompt_text,1,160) AS preview,
                 m.body_vec <=> (SELECT body_vec FROM dashboard.messages WHERE uuid=$1)
                   AS distance
            FROM dashboard.messages m
           WHERE m.uuid != $1 AND m.type='user' AND m.body_vec IS NOT NULL
           ORDER BY distance
           LIMIT $2
        """, (uuid, int(neighbours)))
        out["similar_prompts"] = [
            {"uuid": r[0], "session_id": r[1], "project_slug": r[2],
             "timestamp": r[3], "preview": r[4], "distance": float(r[5])}
            for r in cur.fetchall()
        ]
    else:
        out["similar_prompts"] = []
    return out


def message_count() -> int:
    """Quick health check used by /api/pulse."""
    conn = get_conn()
    if conn is None:
        return 0
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM dashboard.messages")
        return int(cur.fetchone()[0])
    except Exception:
        return 0


def stats() -> dict:
    """Aggregate counts for /api/pulse and the MCP tab's HeliosDB panel."""
    conn = get_conn()
    if conn is None:
        return {"configured": False}
    out: dict = {"configured": True, "ok": True}
    cur = conn.cursor()
    for label, sql in [
        ("messages",        "SELECT COUNT(*) FROM dashboard.messages"),
        ("messages_with_embedding", "SELECT COUNT(*) FROM dashboard.messages WHERE body_vec IS NOT NULL"),
        ("tool_calls",      "SELECT COUNT(*) FROM dashboard.tool_calls"),
        ("tool_results",    "SELECT COUNT(*) FROM dashboard.tool_results"),
        ("tool_results_with_embedding",
                            "SELECT COUNT(*) FROM dashboard.tool_results WHERE body_vec IS NOT NULL"),
        ("docs",            "SELECT COUNT(*) FROM dashboard.docs"),
        ("clusters",        "SELECT COUNT(*) FROM dashboard.clusters"),
        ("alerts_open",     "SELECT COUNT(*) FROM dashboard.alerts WHERE acknowledged_at IS NULL"),
        ("mcp_replays",     "SELECT COUNT(*) FROM dashboard.mcp_replay"),
    ]:
        try:
            cur.execute(sql)
            out[label] = int(cur.fetchone()[0])
        except Exception as e:
            out[label] = {"error": str(e)[:160]}
    return out


_CACHED_SERVER_VERSION: Optional[str] = None
_SERVER_VERSION_LOCK = threading.Lock()


def fetch_server_version() -> str:
    """Query database version once from the DB and cache for the app lifecycle."""
    global _CACHED_SERVER_VERSION
    if _CACHED_SERVER_VERSION is not None:
        return _CACHED_SERVER_VERSION
    with _SERVER_VERSION_LOCK:
        if _CACHED_SERVER_VERSION is not None:
            return _CACHED_SERVER_VERSION
        if not is_configured():
            _CACHED_SERVER_VERSION = ""
            return _CACHED_SERVER_VERSION
        try:
            conn = get_conn()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT version()")
                row = cur.fetchone()
                _CACHED_SERVER_VERSION = str(row[0]) if row and row[0] else ""
        except Exception:
            _CACHED_SERVER_VERSION = ""
        return _CACHED_SERVER_VERSION


def get_nano_version() -> str:
    """Return parsed Nano version like 'v4.41.0' or '' if not detectable."""
    raw = fetch_server_version()
    if not raw:
        return ""
    import re
    m = re.search(r"HeliosDB Nano\s+([\d.]+)", raw)
    if m:
        return f"v{m.group(1)}"
    m2 = re.search(r"Nano\s+([\d.]+)", raw)
    if m2:
        return f"v{m2.group(1)}"
    return ""


def get_powered_by_message() -> str:
    """Dynamic 'powered by HeliosDB-Nano [version]' string loaded once from DB."""
    v = get_nano_version()
    return f"powered by HeliosDB-Nano {v}" if v else "powered by HeliosDB-Nano"

