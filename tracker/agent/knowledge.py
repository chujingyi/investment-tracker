"""RAG v0: ingest framework documents into knowledge_chunks, retrieve via FTS5.

Design notes
- Sources of truth: the four investment-framework markdown docs plus the
  recommendation entry/execution loop rules, all stored in the repo root.
- Chunks are split on level-2 headings so every retrieved piece carries a
  meaningful title; the AIGC YAML frontmatter present in those files is
  stripped before ingestion.
- Retrieval goes through one function (`retrieve`) so the implementation can
  be swapped for embedding search in P1/P2 without touching callers.
- If SQLite FTS5 is unavailable, a LIKE-based fallback keeps the agent usable.
"""

import os
import re
from datetime import datetime, date

REPO_ROOT = os.environ.get("AGENT_KNOWLEDGE_DIR") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

FRAMEWORK_DOCS = [
    "AI超级周期轮动框架.md",
    "供应链瓶颈投资框架.md",
    "动量催化框架.md",
    "NAV折价套利框架.md",
    "推荐录入与执行闭环规则.md",
]

FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)

MAX_CHUNK_CHARS = 2400
MAX_RESULT_CHARS = 900


def _now():
    return datetime.now().isoformat(timespec="seconds")


def strip_frontmatter(text):
    """Remove the leading YAML frontmatter block (AIGC watermark) if present."""
    return FRONTMATTER_RE.sub("", text, count=1)


def split_sections(markdown):
    """Split markdown into (section_title, content) pairs on level-2 headings.

    Content before the first heading becomes a chunk named by the document.
    """
    text = strip_frontmatter(markdown).strip()
    if not text:
        return []
    matches = list(SECTION_RE.finditer(text))
    chunks = []
    if not matches:
        chunks.append(("", text))
        return chunks
    if matches[0].start() > 0:
        intro = text[: matches[0].start()].strip()
        if intro:
            chunks.append(("", intro))
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            chunks.append((title, content))
    return chunks


def _truncate(content, limit=MAX_CHUNK_CHARS):
    if len(content) <= limit:
        return content
    return content[:limit] + "\n…（截断）"


def _fts_available(conn):
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _rebuild_fts(conn):
    if not _fts_available(conn):
        return
    try:
        conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
    except Exception:
        # External-content rebuild is best effort; LIKE fallback still works.
        pass


def ingest_framework_docs(conn, repo_root=REPO_ROOT):
    """(Re)ingest the framework markdown files. Returns count of chunks stored."""
    created = _now()
    stored = 0
    for filename in FRAMEWORK_DOCS:
        path = os.path.join(repo_root, filename)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                markdown = handle.read()
        except OSError:
            continue
        conn.execute(
            "DELETE FROM knowledge_chunks WHERE source=?",
            (f"framework:{filename}",),
        )
        for section, content in split_sections(markdown):
            conn.execute(
                "INSERT INTO knowledge_chunks (title, source, section, content, created_at) "
                "VALUES (?,?,?,?,?)",
                (filename[:-3], f"framework:{filename}", section, _truncate(content), created),
            )
            stored += 1
    conn.commit()
    _rebuild_fts(conn)
    return stored


def ingest_kb_articles(conn):
    """Mirror knowledge_articles into chunks so they are searchable together."""
    created = _now()
    rows = conn.execute(
        "SELECT id, title, category, content FROM knowledge_articles ORDER BY id"
    ).fetchall()
    conn.execute("DELETE FROM knowledge_chunks WHERE source LIKE 'kb:%'")
    stored = 0
    for row in rows:
        content = (row["content"] or "").strip()
        if not content:
            continue
        conn.execute(
            "INSERT INTO knowledge_chunks (title, source, section, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (row["title"], f"kb:{row['id']}", row["category"], _truncate(content), created),
        )
        stored += 1
    conn.commit()
    _rebuild_fts(conn)
    return stored


def ingest_all(conn, repo_root=REPO_ROOT):
    framework = ingest_framework_docs(conn, repo_root)
    articles = ingest_kb_articles(conn)
    return {"framework_chunks": framework, "kb_chunks": articles}


def _sanitize_query(query):
    """Build an FTS5 MATCH expression: keep word-ish tokens, join with OR."""
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", query)
    if not tokens:
        return None
    # Deduplicate while preserving order; cap at 6 tokens.
    seen = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return " OR ".join(f'"{token}"' for token in seen[:6])


def _fts_search(conn, match_expr, k):
    rows = conn.execute(
        "SELECT knowledge_chunks.id AS cid, title, source, section, content "
        "FROM knowledge_fts JOIN knowledge_chunks ON knowledge_chunks.id = knowledge_fts.rowid "
        "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
        (match_expr, k),
    ).fetchall()
    return [dict(row) for row in rows]


def _like_search(conn, query, k):
    """Fallback when FTS5 is unavailable: naive token LIKE ranking."""
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", query)[:6]
    if not tokens:
        return []
    clauses = []
    params = []
    for token in tokens:
        clauses.append("(content LIKE ? OR title LIKE ? OR section LIKE ?)")
        like = f"%{token}%"
        params.extend([like, like, like])
    rows = conn.execute(
        "SELECT id AS cid, title, source, section, content FROM knowledge_chunks "
        f"WHERE {' OR '.join(clauses)}",
        params,
    ).fetchall()
    scored = []
    for row in rows:
        blob = f"{row['title']} {row['section']} {row['content']}"
        score = sum(blob.count(token) for token in tokens)
        scored.append((score, dict(row)))
    scored.sort(key=lambda pair: -pair[0])
    return [row for _, row in scored[:k]]


def retrieve(conn, query, k=5):
    """The single retrieval entry point (swap for embeddings in P1/P2)."""
    k = max(1, min(int(k or 5), 10))
    results = []
    match_expr = _sanitize_query(query)
    if match_expr and _fts_available(conn):
        try:
            results = _fts_search(conn, match_expr, k)
        except Exception:
            results = []
    if not results:
        results = _like_search(conn, query, k)
    for item in results:
        item["content"] = item["content"][:MAX_RESULT_CHARS]
    return results


def chunk_count(conn):
    row = conn.execute("SELECT COUNT(*) AS cnt FROM knowledge_chunks").fetchone()
    return row["cnt"] if row else 0
