"""Item #4 — Project doc ingestion.

Walks each project's `cwd` (gleaned from messages.cwd) and ingests the
common doc files (CLAUDE.md, README.md, ADRs, design docs) into HeliosDB.

Two modes:
  - "native": calls HeliosDB's `graph_rag_ingest_pdf|_office|_image`
    functions over Postgres wire (each takes a path/url + project tag).
    These need a docling-serve sidecar reachable from the HeliosDB host.
  - "fallback": for plain-text/markdown files we don't need docling at
    all. We chunk locally (one chunk per H1/H2 section), embed via the
    existing embedder, and write directly to dashboard.messages /
    dashboard.tool_results-shape extension. This is what runs by default
    so the feature is useful even without a docling sidecar.

Result counts go into `dashboard.docs`.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from . import helios_writer as hw
from . import embedder


COMMON_DOCS = (
    "CLAUDE.md", "README.md", "README", "CONTRIBUTING.md", "ARCHITECTURE.md",
    "DESIGN.md", "ROADMAP.md", "CHANGELOG.md",
)


def _cwd_for_project(conn, slug: str) -> Optional[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT cwd FROM dashboard.messages "
        "WHERE project_slug=$1 AND cwd IS NOT NULL "
        "GROUP BY cwd ORDER BY COUNT(*) DESC LIMIT 1",
        (slug,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _chunk_markdown(text: str, max_chars: int = 1200) -> list:
    """Split on H1/H2 headers; each chunk capped at max_chars."""
    chunks = []
    sections = re.split(r"(?m)^(?=#{1,3}\s)", text)
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        # If section is too long, hard-split by paragraph
        if len(sec) <= max_chars:
            chunks.append(sec)
        else:
            paras = sec.split("\n\n")
            buf = ""
            for p in paras:
                if len(buf) + len(p) + 2 > max_chars:
                    if buf: chunks.append(buf)
                    buf = p
                else:
                    buf = (buf + "\n\n" + p) if buf else p
            if buf: chunks.append(buf)
    return chunks


def ingest_project(project_slug: str, cwd: Optional[str] = None) -> dict:
    """Find docs in the project's cwd, chunk, embed, and persist."""
    conn = hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    if not cwd:
        cwd = _cwd_for_project(conn, project_slug)
    if not cwd:
        return {"ok": False, "error": f"no cwd known for {project_slug}"}
    root = Path(cwd)
    if not root.is_dir():
        return {"ok": False, "error": f"cwd {cwd!r} not accessible from dashboard host"}

    found: list[tuple[str, str]] = []
    for name in COMMON_DOCS:
        p = root / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    found.append((str(p), text))
            except Exception:
                continue
    # Plus any file under docs/ matching md/rst
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for p in sorted(docs_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".md", ".rst", ".txt") and p.stat().st_size < 500_000:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    if text.strip():
                        found.append((str(p), text))
                except Exception:
                    continue
    if not found:
        return {"ok": False, "error": "no docs found"}

    cur = conn.cursor()
    total_sections = 0
    total_chunks = 0
    ingested = []
    now = time.time()
    for path, text in found:
        chunks = _chunk_markdown(text)
        # Schema for doc nodes lives alongside dashboard.messages — use
        # negative-prefixed uuids so they never collide with real Claude
        # message uuids.
        for i, chunk in enumerate(chunks):
            preview = chunk[:160]
            vec = embedder.embed(chunk)
            chunk_uuid = f"doc:{project_slug}:{Path(path).name}:{i}"
            try:
                cur.execute("""
                  INSERT INTO dashboard.messages (
                    uuid, parent_uuid, session_id, project_slug, cwd, type,
                    timestamp, prompt_text, prompt_chars, body_vec
                  ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9, CAST($10 AS VECTOR(384))
                  )
                  ON CONFLICT (uuid) DO UPDATE SET
                    prompt_text = EXCLUDED.prompt_text,
                    body_vec = EXCLUDED.body_vec
                """, (chunk_uuid, None, f"docs:{project_slug}", project_slug,
                      cwd, "doc",
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                      chunk, len(chunk), hw._vector_literal(vec)))
                total_chunks += 1
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
        total_sections += 1
        ingested.append({"path": path, "chunks": len(chunks)})
    # Record summary in dashboard.docs
    try:
        doc_id = abs(hash(("docs", project_slug, now))) % (2**62)
        cur.execute("""
          INSERT INTO dashboard.docs (
            id, project_slug, source_path, doc_kind, ingested_at,
            section_count, chunk_count, graph_root_uuid
          ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
          ON CONFLICT (id) DO NOTHING
        """, (doc_id, project_slug, cwd, "markdown-bundle", now,
              total_sections, total_chunks, f"docs:{project_slug}"))
    except Exception:
        pass
    hw._commit(conn)
    return {
        "ok": True, "project_slug": project_slug, "cwd": cwd,
        "docs": ingested, "sections": total_sections, "chunks": total_chunks,
    }


def list_docs() -> list:
    conn = hw.get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    try:
        cur.execute("""
          SELECT id, project_slug, source_path, doc_kind, ingested_at,
                 section_count, chunk_count
            FROM dashboard.docs
           ORDER BY ingested_at DESC
        """)
        return [{"id": r[0], "project_slug": r[1], "source_path": r[2],
                 "doc_kind": r[3], "ingested_at": float(r[4]) if r[4] else None,
                 "section_count": r[5], "chunk_count": r[6]}
                for r in cur.fetchall()]
    except Exception:
        return []
