"""Item #8 — Live "currently expensive" alerts.

Watches `dashboard.messages` for newly-inserted assistant turns whose
billable token count crosses a threshold and emits an entry in
`dashboard.alerts`. The dashboard's existing SSE stream picks them up.

We don't depend on Postgres LISTEN/NOTIFY — each scan iteration polls
for new turns above threshold. That's good enough at the dashboard's
scan cadence (60 min) and avoids needing a long-lived PG connection.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from . import helios_writer as hw


DEFAULT_THRESHOLD = int(os.environ.get("TD_ALERT_THRESHOLD", "100000"))


def scan_for_alerts(threshold: int = DEFAULT_THRESHOLD) -> list:
    """Find assistant turns above threshold that don't yet have an alert row.
    Emits one alert per offending turn."""
    conn = hw.get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    # HeliosDB v3.26 has issues projecting CTE results into outer queries
    # with parameter binding — write the comparison inline.
    try:
        cur.execute("""
          SELECT m.uuid, m.session_id,
                 COALESCE(m.input_tokens,0)+COALESCE(m.output_tokens,0)
                   +COALESCE(m.cache_create_5m_tokens,0)
                   +COALESCE(m.cache_create_1h_tokens,0) AS billable
            FROM dashboard.messages m
            LEFT JOIN dashboard.alerts a ON a.message_uuid = m.uuid AND a.kind='expensive_turn'
           WHERE m.type='assistant'
             AND a.id IS NULL
             AND COALESCE(m.input_tokens,0)+COALESCE(m.output_tokens,0)
                  +COALESCE(m.cache_create_5m_tokens,0)
                  +COALESCE(m.cache_create_1h_tokens,0) >= $1
           ORDER BY billable DESC
           LIMIT 100
        """, (int(threshold),))
        offenders = cur.fetchall()
    except Exception as e:
        return []
    new_alerts = []
    now = time.time()
    for uuid, sess, billable in offenders:
        # Use a hash-derived id so retries are idempotent
        aid = abs(hash(("expensive_turn", uuid))) % (2**62)
        try:
            cur.execute("""
              INSERT INTO dashboard.alerts (id, kind, session_id, message_uuid,
                                            threshold, observed, created_at)
              VALUES ($1,$2,$3,$4,$5,$6,$7)
              ON CONFLICT (id) DO NOTHING
            """, (aid, "expensive_turn", sess, uuid, int(threshold),
                  int(billable), now))
            new_alerts.append({"id": aid, "session_id": sess,
                               "message_uuid": uuid, "billable": int(billable),
                               "threshold": int(threshold)})
        except Exception:
            continue
    hw._commit(conn)
    return new_alerts


def list_alerts(only_open: bool = True, limit: int = 50) -> list:
    conn = hw.get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    where = "acknowledged_at IS NULL" if only_open else "1=1"
    try:
        cur.execute(f"""
          SELECT id, kind, session_id, message_uuid, threshold, observed,
                 created_at, acknowledged_at
            FROM dashboard.alerts
           WHERE {where}
           ORDER BY created_at DESC
           LIMIT $1
        """, (int(limit),))
        return [
            {"id": r[0], "kind": r[1], "session_id": r[2], "message_uuid": r[3],
             "threshold": r[4], "observed": r[5],
             "created_at": float(r[6]) if r[6] else None,
             "acknowledged_at": float(r[7]) if r[7] else None}
            for r in cur.fetchall()
        ]
    except Exception:
        return []


def acknowledge(alert_id: int) -> bool:
    conn = hw.get_conn()
    if conn is None:
        return False
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE dashboard.alerts SET acknowledged_at = $1 WHERE id = $2",
            (time.time(), int(alert_id)),
        )
        hw._commit(conn)
        return True
    except Exception:
        return False
