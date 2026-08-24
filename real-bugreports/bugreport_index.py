#!/usr/bin/env python3
"""Streaming Android bugreport ZIP chunker and SQLite search CLI."""

from __future__ import annotations

import argparse
import array
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = "1"
SECTION_RE = re.compile(r"^------\s+(.+?)\s+------\s*$")
DURATION_RE = re.compile(r"^------\s+[\d.]+s was the duration of")
BUFFER_RE = re.compile(r"^-{5,}\s+beginning of\s+(.+?)\s*$", re.I)
THREADTIME_RE = re.compile(
    r"^(?P<time>\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+)\s+"
    r"(?:\d+\s+)?(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<sev>[VDIWEFAS])\s+(?P<tag>[^:]+):"
)
BRIEF_RE = re.compile(r"^(?P<sev>[VDIWEFAS])/(?P<tag>[^\(]+)\(\s*(?P<pid>\d+)\):")
FULL_TIME_RE = re.compile(r"\b(20\d\d-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?)\b")
PID_RE = re.compile(r"\bpid[=: ]+(\d+)\b", re.I)
PROCESS_RE = re.compile(r"^(?:Process|Cmd line):\s*(\S+)", re.I)
STACK_RE = re.compile(
    r"(?:FATAL EXCEPTION|java\.lang\.|Caused by:|^\s*at\s+[\w.$]+\(|^\s*#\d+\s+pc\s+|backtrace:|stack trace)", re.I
)
SEVERITY_ORDER = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4, "F": 5, "A": 6}
OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "gemma4:e4b"
DEFAULT_EMBEDDING_MODEL = "embeddinggemma:latest"


@dataclass
class Chunk:
    section: str
    start_line: int
    lines: list[str] = field(default_factory=list)
    kind: str = "text"

    @property
    def end_line(self) -> int:
        return self.start_line + len(self.lines) - 1


def decode_line(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").rstrip("\r\n")


def resolve_main_entry(zf: zipfile.ZipFile) -> str:
    candidates = [n for n in zf.namelist() if n == "main_entry.txt" or n.endswith("/main_entry.txt")]
    if not candidates:
        raise ValueError("ZIP에 main_entry.txt가 없습니다")
    with zf.open(candidates[0]) as stream:
        target = stream.read(65536).decode("utf-8", "replace").strip().replace("\\", "/")
    if not target:
        raise ValueError("main_entry.txt가 비어 있습니다")
    names = set(zf.namelist())
    if target in names:
        return target
    suffix = "/" + target.lstrip("/")
    matches = [n for n in names if n.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"main_entry.txt 대상이 ZIP에 없습니다: {target}")


def is_section(line: str) -> str | None:
    match = SECTION_RE.match(line)
    if not match or DURATION_RE.match(line):
        return None
    title = match.group(1).strip()
    if not title or set(title) <= {"-"}:
        return None
    return title[:500]


def iter_chunks(lines: Iterable[bytes], max_lines: int = 200) -> Iterator[Chunk]:
    """One-pass chunking. Section boundaries and log/stack records are kept when practical."""
    section = "PREAMBLE"
    current: Chunk | None = None
    stack_grace = 0

    def flush() -> Chunk | None:
        nonlocal current, stack_grace
        out = current
        current = None
        stack_grace = 0
        return out

    for number, raw in enumerate(lines, 1):
        line = decode_line(raw)
        new_section = is_section(line)
        buffer = BUFFER_RE.match(line)
        if new_section:
            old = flush()
            if old:
                yield old
            section = new_section
            current = Chunk(section, number, [line], "section")
            continue
        if buffer:
            old = flush()
            if old:
                yield old
            section = f"{section} / {buffer.group(1).strip()}"
            current = Chunk(section, number, [line], "log")
            continue

        log_match = THREADTIME_RE.match(line) or BRIEF_RE.match(line)
        stack_line = bool(STACK_RE.search(line))
        if current is None:
            current = Chunk(section, number, [], "log" if log_match else "text")
        elif log_match and current.lines and current.kind not in ("section", "log", "stack"):
            old = flush()
            if old:
                yield old
            current = Chunk(section, number, [], "log")
        elif stack_line and current.lines and current.kind != "stack":
            old = flush()
            if old:
                yield old
            current = Chunk(section, number, [], "stack")

        current.lines.append(line)
        if stack_line:
            current.kind = "stack"
            stack_grace = 12
        elif stack_grace:
            stack_grace -= 1

        limit = max_lines * (2 if current.kind == "stack" else 1)
        if len(current.lines) >= limit and stack_grace == 0:
            old = flush()
            if old:
                yield old
    old = flush()
    if old:
        yield old


def metadata(chunk: Chunk) -> dict[str, str | int]:
    first_time = ""
    last_time = ""
    pids: set[str] = set()
    processes: set[str] = set()
    severities: set[str] = set()
    stack = chunk.kind == "stack"
    for line in chunk.lines:
        match = THREADTIME_RE.match(line) or BRIEF_RE.match(line)
        if match:
            values = match.groupdict()
            timestamp = values.get("time") or ""
            if timestamp:
                first_time = first_time or timestamp
                last_time = timestamp
            if values.get("pid"):
                pids.add(values["pid"])
            if values.get("sev"):
                severities.add(values["sev"])
            tag = (values.get("tag") or "").strip()
            if tag:
                processes.add(tag)
        if not first_time:
            tm = FULL_TIME_RE.search(line)
            if tm:
                first_time = last_time = tm.group(1)
        pm = PROCESS_RE.match(line)
        if pm:
            processes.add(pm.group(1))
        if len(pids) < 12:
            pids.update(PID_RE.findall(line))
        stack = stack or bool(STACK_RE.search(line))
    severity = max(severities, key=lambda s: SEVERITY_ORDER.get(s, -1)) if severities else ""
    return {
        "timestamp_start": first_time,
        "timestamp_end": last_time,
        "process": ",".join(sorted(processes)[:12]),
        "pid": ",".join(sorted(pids, key=lambda x: int(x))[:12]),
        "severity": severity,
        "is_stack": int(stack),
    }


def create_schema(conn: sqlite3.Connection) -> bool:
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE reports(
      id INTEGER PRIMARY KEY, zip_path TEXT NOT NULL UNIQUE, zip_name TEXT NOT NULL,
      zip_size INTEGER NOT NULL, zip_mtime_ns INTEGER NOT NULL, main_entry TEXT NOT NULL,
      main_size INTEGER NOT NULL, indexed_at TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE chunks(
      id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id), seq INTEGER NOT NULL,
      section TEXT NOT NULL, source_entry TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
      kind TEXT NOT NULL, timestamp_start TEXT, timestamp_end TEXT, process TEXT, pid TEXT,
      severity TEXT, is_stack INTEGER NOT NULL, content TEXT NOT NULL,
      UNIQUE(report_id, seq)
    );
    CREATE INDEX chunks_report_seq ON chunks(report_id, seq);
    CREATE INDEX chunks_section ON chunks(section);
    """)
    try:
        conn.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(content, section, process, content='chunks', content_rowid='id')")
        fts = True
    except sqlite3.OperationalError:
        fts = False
    conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    conn.execute("INSERT INTO meta VALUES('fts5', ?)", ("1" if fts else "0",))
    return fts


def index_zip(conn: sqlite3.Connection, path: Path, max_lines: int, fts: bool) -> tuple[int, int, str]:
    stat = path.stat()
    with zipfile.ZipFile(path) as zf:
        main = resolve_main_entry(zf)
        info = zf.getinfo(main)
        cur = conn.execute(
            "INSERT INTO reports(zip_path,zip_name,zip_size,zip_mtime_ns,main_entry,main_size,indexed_at) VALUES(?,?,?,?,?,?,?)",
            (str(path.resolve()), path.name, stat.st_size, stat.st_mtime_ns, main, info.file_size,
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        report_id = cur.lastrowid
        count = 0
        with zf.open(main) as stream:
            for seq, chunk in enumerate(iter_chunks(stream, max_lines), 1):
                meta = metadata(chunk)
                content = "\n".join(chunk.lines)
                cur = conn.execute("""
                  INSERT INTO chunks(report_id,seq,section,source_entry,start_line,end_line,kind,
                    timestamp_start,timestamp_end,process,pid,severity,is_stack,content)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (report_id, seq, chunk.section, main, chunk.start_line, chunk.end_line, chunk.kind,
                      meta["timestamp_start"], meta["timestamp_end"], meta["process"], meta["pid"],
                      meta["severity"], meta["is_stack"], content))
                if fts:
                    conn.execute("INSERT INTO chunks_fts(rowid,content,section,process) VALUES(?,?,?,?)",
                                 (cur.lastrowid, content, chunk.section, meta["process"]))
                count = seq
        conn.execute("UPDATE reports SET chunk_count=? WHERE id=?", (count, report_id))
    return report_id, count, main


def find_zips(inputs: list[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for value in inputs:
        path = Path(value).resolve()
        candidates = sorted(path.glob("*.zip")) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() == ".zip":
                found[str(candidate).lower()] = candidate
    return sorted(found.values(), key=lambda p: p.name.lower())


def command_index(args: argparse.Namespace) -> int:
    zips = find_zips(args.inputs)
    if not zips:
        print("인덱싱할 ZIP이 없습니다.", file=sys.stderr)
        return 2
    output = Path(args.db).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + f".tmp-{os.getpid()}")
    if temp.exists():
        temp.unlink()
    started = time.monotonic()
    try:
        # sqlite3.Connection's context manager commits but does not close.  An
        # explicit closing context is required before os.replace on Windows.
        with contextlib.closing(sqlite3.connect(temp)) as conn:
            with conn:
                fts = create_schema(conn)
                for path in zips:
                    before = time.monotonic()
                    _, count, main = index_zip(conn, path, args.max_lines, fts)
                    conn.commit()
                    print(f"indexed: {path.name} | entry={main} | chunks={count} | {time.monotonic()-before:.2f}s")
                conn.execute("INSERT INTO meta VALUES('created_at', ?)", (dt.datetime.now(dt.timezone.utc).isoformat(),))
                conn.commit()
        os.replace(temp, output)
    except Exception:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    print(f"database: {output} | reports={len(zips)} | fts5={'on' if fts else 'off'} | total={time.monotonic()-started:.2f}s")
    return 0


def query_terms(text: str) -> list[str]:
    return [token for token in re.findall(r"[\w.:/$-]+", text, re.UNICODE) if token]


def ollama_json(base_url: str, path: str, payload: dict | None = None, timeout: int = 10) -> dict:
    url = base_url.rstrip("/") + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"Ollama HTTP 오류 {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Ollama에 연결할 수 없습니다: {base_url}. Ollama가 설치되어 실행 중인지 확인하세요. ({exc})"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Ollama가 올바른 JSON 응답을 반환하지 않았습니다.") from exc


def installed_models(base_url: str, timeout: int = 10) -> list[str]:
    response = ollama_json(base_url, "/api/tags", timeout=timeout)
    return [item.get("name", "") for item in response.get("models", []) if item.get("name")]


def choose_model(models: list[str], requested: str | None) -> str:
    if not models:
        raise RuntimeError("Ollama에 설치된 모델이 없습니다. 모델을 자동 다운로드하지 않습니다. 관리자가 모델을 준비해야 합니다.")
    selected = requested or DEFAULT_OLLAMA_MODEL
    if selected not in models:
        label = "기본 모델" if requested is None else "요청한 모델"
        raise RuntimeError(f"{label} '{selected}'이 설치되어 있지 않습니다. 설치 모델: {', '.join(models)}")
    return selected


def resolve_embedding_model(models: list[str], requested: str) -> str:
    candidates = (requested, requested + ":latest") if ":" not in requested else (requested,)
    for candidate in candidates:
        if candidate in models:
            return candidate
    raise RuntimeError(f"임베딩 모델 '{requested}'이 설치되어 있지 않습니다. 설치 모델: {', '.join(models) or '(없음)'}")


def embed_texts(base_url: str, model: str, texts: list[str], timeout: int) -> list[list[float]]:
    response = ollama_json(base_url, "/api/embed", {"model": model, "input": texts}, timeout)
    vectors = response.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RuntimeError("Ollama 임베딩 응답의 벡터 개수가 요청과 일치하지 않습니다.")
    dimensions = {len(vector) for vector in vectors if isinstance(vector, list)}
    if len(dimensions) != 1 or not dimensions or 0 in dimensions:
        raise RuntimeError("Ollama 임베딩 응답의 차원이 올바르지 않습니다.")
    return vectors


def normalized_blob(vector: list[float]) -> tuple[bytes, float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise RuntimeError("0 또는 비정상 임베딩 벡터를 받았습니다.")
    values = array.array("f", (float(value) / norm for value in vector))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes(), norm


def blob_vector(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def command_embed_index(args: argparse.Namespace) -> int:
    source = Path(args.db).resolve()
    output = Path(args.output).resolve()
    if source == output:
        raise RuntimeError("기존 DB 보호를 위해 --output은 --db와 다른 경로여야 합니다.")
    models = installed_models(args.ollama_url, min(args.timeout, 10))
    model = resolve_embedding_model(models, args.embedding_model)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + f".tmp-{os.getpid()}")
    with contextlib.suppress(OSError):
        temp.unlink()
    started = time.monotonic()
    try:
        with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
            with contextlib.closing(sqlite3.connect(temp)) as dst:
                src.backup(dst)
                dst.executescript("""
                  CREATE TABLE IF NOT EXISTS embeddings(
                    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id), model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL, norm REAL NOT NULL, content_chars INTEGER NOT NULL,
                    vector BLOB NOT NULL
                  );
                  CREATE INDEX IF NOT EXISTS embeddings_model ON embeddings(model);
                  DELETE FROM embeddings;
                """)
                total = dst.execute("SELECT count(*) FROM chunks").fetchone()[0]
                cursor = dst.execute("SELECT id,section,content FROM chunks ORDER BY id")
                done = 0
                dimensions = 0
                while True:
                    rows = cursor.fetchmany(args.batch_size)
                    if not rows:
                        break
                    texts = [f"search_document: {row[1]}\n{row[2][:args.max_embed_chars]}" for row in rows]
                    vectors = embed_texts(args.ollama_url, model, texts, args.timeout)
                    records = []
                    for row, text, vector in zip(rows, texts, vectors):
                        blob, norm = normalized_blob(vector)
                        dimensions = len(vector)
                        records.append((row[0], model, dimensions, norm, len(text), blob))
                    dst.executemany("INSERT INTO embeddings VALUES(?,?,?,?,?,?)", records)
                    dst.commit()
                    done += len(rows)
                    if done == total or done % max(args.batch_size * 25, 1) == 0:
                        print(f"embedded: {done}/{total} ({done/total:.1%})", flush=True)
                dst.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embedding_model',?)", (model,))
                dst.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embedding_dimensions',?)", (str(dimensions),))
                dst.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embedding_count',?)", (str(done),))
                dst.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embedding_created_at',?)",
                            (dt.datetime.now(dt.timezone.utc).isoformat(),))
                dst.commit()
        os.replace(temp, output)
    except Exception:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    print(f"hybrid database: {output} | model={model} | dimensions={dimensions} | embeddings={done} | total={time.monotonic()-started:.2f}s")
    return 0


def hybrid_matches(conn: sqlite3.Connection, question: str, limit: int, base_url: str,
                   embedding_model: str, timeout: int, candidate_limit: int = 80) -> list[sqlite3.Row]:
    terms = query_terms(question)
    if not terms:
        raise RuntimeError("질문에서 검색 키워드를 찾지 못했습니다.")
    terms = terms[:12]
    fts = conn.execute("SELECT value FROM meta WHERE key='fts5'").fetchone()[0] == "1"
    if fts:
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        lexical = conn.execute("""SELECT c.*,r.zip_name,bm25(chunks_fts) score
          FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN reports r ON r.id=c.report_id
          WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (expression, candidate_limit)).fetchall()
    else:
        likes = " OR ".join("(c.content LIKE ? OR c.section LIKE ? OR c.process LIKE ?)" for _ in terms)
        params = [item for term in terms for item in (f"%{term}%",) * 3]
        lexical = conn.execute(f"""SELECT c.*,r.zip_name,0 score FROM chunks c JOIN reports r ON r.id=c.report_id
          WHERE {likes} ORDER BY c.id LIMIT ?""", (*params, candidate_limit)).fetchall()

    stored = conn.execute("SELECT value FROM meta WHERE key='embedding_model'").fetchone()
    if not stored:
        return lexical[:limit]
    stored_model = stored[0]
    if embedding_model and embedding_model not in (stored_model, stored_model.removesuffix(":latest")):
        raise RuntimeError(f"DB 임베딩 모델은 '{stored_model}'입니다. 같은 모델로 질의하거나 재인덱싱하세요.")
    query_vector = embed_texts(base_url, stored_model, ["search_query: " + question], timeout)[0]
    query_blob, _ = normalized_blob(query_vector)
    query_values = blob_vector(query_blob)
    semantic_scores: list[tuple[float, int]] = []
    for chunk_id, dimensions, blob in conn.execute("SELECT chunk_id,dimensions,vector FROM embeddings WHERE model=?", (stored_model,)):
        values = blob_vector(blob)
        if dimensions != len(query_values) or len(values) != len(query_values):
            continue
        score = sum(a * b for a, b in zip(query_values, values))
        semantic_scores.append((score, chunk_id))
    semantic_scores.sort(reverse=True)
    ranks: dict[int, float] = {}
    for rank, row in enumerate(lexical, 1):
        ranks[row["id"]] = ranks.get(row["id"], 0.0) + 1.0 / (60 + rank)
    for rank, (_, chunk_id) in enumerate(semantic_scores[:candidate_limit], 1):
        ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (60 + rank)
    best = sorted(ranks, key=lambda chunk_id: ranks[chunk_id], reverse=True)[:limit]
    if not best:
        return []
    placeholders = ",".join("?" for _ in best)
    fetched = conn.execute(f"""SELECT c.*,r.zip_name,0 score FROM chunks c JOIN reports r ON r.id=c.report_id
      WHERE c.id IN ({placeholders})""", best).fetchall()
    by_id = {row["id"]: row for row in fetched}
    return [by_id[chunk_id] for chunk_id in best if chunk_id in by_id]


def retrieve_for_ask(db: str, question: str, limit: int, context: int,
                     base_url: str = OLLAMA_DEFAULT_URL, embedding_model: str = DEFAULT_EMBEDDING_MODEL,
                     timeout: int = 300) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        fts = conn.execute("SELECT value FROM meta WHERE key='fts5'").fetchone()[0] == "1"
        has_embeddings = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone()
        if has_embeddings and conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone():
            matches = hybrid_matches(conn, question, limit, base_url, embedding_model, timeout)
            terms = []
        else:
            terms = query_terms(question)
        if not terms and not has_embeddings:
            raise RuntimeError("질문에서 검색 키워드를 찾지 못했습니다.")
        # Natural questions contain particles and filler words. OR retrieval is
        # deliberately used here; BM25 still ranks chunks matching more terms.
        terms = terms[:12]
        if has_embeddings and not terms:
            pass
        elif fts:
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            matches = conn.execute("""SELECT c.*,r.zip_name,bm25(chunks_fts) score
              FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN reports r ON r.id=c.report_id
              WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (expression, limit)).fetchall()
        else:
            likes = " OR ".join("(c.content LIKE ? OR c.section LIKE ? OR c.process LIKE ?)" for _ in terms)
            params = [item for term in terms for item in (f"%{term}%",) * 3]
            matches = conn.execute(f"""SELECT c.*,r.zip_name,0 score FROM chunks c
              JOIN reports r ON r.id=c.report_id WHERE {likes} ORDER BY c.id LIMIT ?""", (*params, limit)).fetchall()
        expanded: dict[int, sqlite3.Row] = {}
        for row in matches:
            neighbors = conn.execute("""SELECT c.*,r.zip_name,NULL score FROM chunks c JOIN reports r ON r.id=c.report_id
              WHERE c.report_id=? AND c.seq BETWEEN ? AND ? ORDER BY c.seq""",
              (row["report_id"], max(1, row["seq"] - context), row["seq"] + context)).fetchall()
            expanded.update({item["id"]: item for item in neighbors})
        return sorted(expanded.values(), key=lambda row: (row["zip_name"], row["seq"]))
    finally:
        conn.close()


def build_evidence(rows: list[sqlite3.Row], max_chars: int, per_chunk_chars: int) -> tuple[str, list[str]]:
    blocks: list[str] = []
    sources: list[str] = []
    used = 0
    for number, row in enumerate(rows, 1):
        source = f"{row['zip_name']}::{row['source_entry']}:{row['start_line']}-{row['end_line']}"
        header = f"[S{number}] 출처={source}\n섹션={row['section']} | 종류={row['kind']}"
        details = []
        for key in ("timestamp_start", "timestamp_end", "process", "pid", "severity"):
            if row[key]:
                details.append(f"{key}={row[key]}")
        content = row["content"][:per_chunk_chars]
        block = header + ("\n" + " | ".join(details) if details else "") + "\n" + content
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining < len(header) + 200:
                break
            block = block[:remaining]
        blocks.append(block)
        sources.append(f"[S{len(sources)+1}] {source}")
        used += len(block)
        if used >= max_chars:
            break
    return "\n\n".join(blocks), sources


def command_ask(args: argparse.Namespace) -> int:
    models = installed_models(args.ollama_url, timeout=min(args.timeout, 10))
    model = choose_model(models, args.model)
    rows = retrieve_for_ask(args.db, args.question, args.limit, args.context,
                            args.ollama_url, args.embedding_model, args.timeout)
    if not rows:
        print("관련 로그 청크를 찾지 못해 LLM을 호출하지 않았습니다.", file=sys.stderr)
        return 1
    evidence, sources = build_evidence(rows, args.max_context_chars, args.per_chunk_chars)
    system = (
        "당신은 Android bugreport 분석가입니다. 제공된 근거만 사용해 한국어로 답하세요. "
        "추측하지 말고 근거가 부족하면 부족하다고 명시하세요. 핵심 판단마다 [S1] 형식으로 인용하고, "
        "원인 후보와 확정 사실을 구분하며 실용적인 다음 확인 항목을 제시하세요."
    )
    prompt = f"질문:\n{args.question}\n\n검색된 bugreport 근거:\n{evidence}"
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "options": {"temperature": 0.1, "num_ctx": args.num_ctx},
    }
    response = ollama_json(args.ollama_url, "/api/chat", payload=payload, timeout=args.timeout)
    answer = (response.get("message") or {}).get("content", "").strip()
    if not answer:
        raise RuntimeError("Ollama 응답에 답변 텍스트가 없습니다.")
    print(f"모델: {model}\n")
    print(answer)
    print("\n검색 근거 출처:")
    for source in sources:
        print(f"- {source}")
    return 0


def command_hybrid_search(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone()
        if not table or not conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone():
            raise RuntimeError("이 DB에는 임베딩이 없습니다. 먼저 embed-index로 별도 하이브리드 DB를 만드세요.")
        rows = hybrid_matches(conn, args.query, args.limit, args.ollama_url,
                              args.embedding_model, args.timeout, args.candidates)
        if not rows:
            print("검색 결과가 없습니다.")
            return 1
        for number, row in enumerate(rows, 1):
            print(f"\n[{number}] HYBRID {row['zip_name']}::{row['source_entry']}:{row['start_line']}-{row['end_line']}")
            print(f"    section={row['section']} | kind={row['kind']} | process={row['process'] or ''} | severity={row['severity'] or ''}")
            lines = row["content"].splitlines()
            shown = lines if args.preview_lines == 0 else lines[:args.preview_lines]
            print("\n".join(shown))
            if args.preview_lines and len(lines) > args.preview_lines:
                print("... (preview truncated)")
        print(f"\nmatched={len(rows)} retrieval=FTS+embedding-RRF model={args.embedding_model}")
        return 0
    finally:
        conn.close()


def command_search(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    fts = conn.execute("SELECT value FROM meta WHERE key='fts5'").fetchone()[0] == "1"
    terms = query_terms(args.query)
    if not terms:
        print("검색어가 비어 있습니다.", file=sys.stderr)
        return 2
    filters: list[str] = []
    params: list[object] = []
    if args.section:
        filters.append("c.section LIKE ?")
        params.append(f"%{args.section}%")
    if args.severity:
        filters.append("c.severity = ?")
        params.append(args.severity.upper())
    where_extra = (" AND " + " AND ".join(filters)) if filters else ""
    if fts:
        expression = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = f"""SELECT c.*,r.zip_name,bm25(chunks_fts) score
          FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN reports r ON r.id=c.report_id
          WHERE chunks_fts MATCH ? {where_extra} ORDER BY score LIMIT ?"""
        rows = conn.execute(sql, [expression, *params, args.limit]).fetchall()
    else:
        likes = " AND ".join("(c.content LIKE ? OR c.section LIKE ? OR c.process LIKE ?)" for _ in terms)
        like_params = [item for term in terms for item in (f"%{term}%",) * 3]
        sql = f"""SELECT c.*,r.zip_name,0 score FROM chunks c JOIN reports r ON r.id=c.report_id
          WHERE {likes} {where_extra} ORDER BY c.id LIMIT ?"""
        rows = conn.execute(sql, [*like_params, *params, args.limit]).fetchall()
    if not rows:
        print("검색 결과가 없습니다.")
        conn.close()
        return 1

    relevant = {row["id"] for row in rows}
    expanded: dict[int, sqlite3.Row] = {row["id"]: row for row in rows}
    for row in rows:
        neighbors = conn.execute("""SELECT c.*,r.zip_name,NULL score FROM chunks c JOIN reports r ON r.id=c.report_id
          WHERE c.report_id=? AND c.seq BETWEEN ? AND ? ORDER BY c.seq""",
          (row["report_id"], max(1, row["seq"] - args.context), row["seq"] + args.context)).fetchall()
        expanded.update({item["id"]: item for item in neighbors})
    ordered = sorted(expanded.values(), key=lambda r: (r["zip_name"], r["seq"]))
    for idx, row in enumerate(ordered, 1):
        marker = "MATCH" if row["id"] in relevant else "CONTEXT"
        meta = [f"section={row['section']}", f"kind={row['kind']}"]
        for key in ("timestamp_start", "process", "pid", "severity"):
            if row[key]:
                meta.append(f"{key}={row[key]}")
        if row["is_stack"]:
            meta.append("stack=yes")
        print(f"\n[{idx}] {marker} {row['zip_name']}::{row['source_entry']}:{row['start_line']}-{row['end_line']} (chunk={row['id']}, seq={row['seq']})")
        print("    " + " | ".join(meta))
        content = row["content"]
        if args.preview_lines and len(content.splitlines()) > args.preview_lines:
            content = "\n".join(content.splitlines()[:args.preview_lines]) + "\n... (preview truncated)"
        print(content)
    print(f"\nmatched={len(rows)} expanded={len(ordered)} context={args.context}")
    conn.close()
    return 0


def command_stats(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT zip_name,main_entry,main_size,chunk_count,indexed_at FROM reports ORDER BY zip_name"):
        print(json.dumps(dict(row), ensure_ascii=False))
    totals = conn.execute("SELECT count(*) reports,coalesce(sum(chunk_count),0) chunks FROM reports").fetchone()
    print(json.dumps({"reports": totals[0], "chunks": totals[1]}, ensure_ascii=False))
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Android bugreport ZIP 스트리밍 인덱서/검색기")
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index", help="ZIP 또는 ZIP 디렉터리를 새 SQLite DB로 인덱싱")
    index.add_argument("inputs", nargs="+", help="ZIP 파일/디렉터리")
    index.add_argument("--db", required=True, help="출력 SQLite 경로(원자적으로 새로 작성)")
    index.add_argument("--max-lines", type=int, default=200, choices=range(50, 2001), metavar="50..2000")
    index.set_defaults(func=command_index)
    embed_index = sub.add_parser("embed-index", help="기존 DB 복사본에 Ollama 청크 임베딩 추가")
    embed_index.add_argument("--db", required=True, help="읽기 전용 원본 FTS DB")
    embed_index.add_argument("--output", required=True, help="새 하이브리드 DB 경로")
    embed_index.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    embed_index.add_argument("--ollama-url", default=OLLAMA_DEFAULT_URL)
    embed_index.add_argument("--batch-size", type=int, default=8, choices=range(1, 65), metavar="1..64")
    embed_index.add_argument("--max-embed-chars", type=int, default=6000)
    embed_index.add_argument("--timeout", type=int, default=300)
    embed_index.set_defaults(func=command_embed_index)
    search = sub.add_parser("search", help="키워드 검색 및 인접 청크 확장")
    search.add_argument("--db", required=True)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--context", type=int, default=1, help="앞뒤 인접 청크 수")
    search.add_argument("--section")
    search.add_argument("--severity", choices=list("VDIWEFAS") + list("vdiwefas"))
    search.add_argument("--preview-lines", type=int, default=30, help="0이면 전체 청크 출력")
    search.set_defaults(func=command_search)
    hybrid = sub.add_parser("hybrid-search", help="FTS와 embeddinggemma 벡터 순위를 결합해 검색")
    hybrid.add_argument("--db", required=True)
    hybrid.add_argument("query")
    hybrid.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    hybrid.add_argument("--ollama-url", default=OLLAMA_DEFAULT_URL)
    hybrid.add_argument("--limit", type=int, default=10)
    hybrid.add_argument("--candidates", type=int, default=80)
    hybrid.add_argument("--preview-lines", type=int, default=20)
    hybrid.add_argument("--timeout", type=int, default=300)
    hybrid.set_defaults(func=command_hybrid_search)
    ask = sub.add_parser("ask", help="검색 근거를 로컬 Ollama에 전달해 한국어 답변 생성")
    ask.add_argument("--db", required=True)
    ask.add_argument("question", help="bugreport에 대해 물을 질문")
    ask.add_argument("--model", help=f"설치된 Ollama 모델(기본: {DEFAULT_OLLAMA_MODEL})")
    ask.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL,
                     help=f"하이브리드 DB 질의 임베딩 모델(기본: {DEFAULT_EMBEDDING_MODEL})")
    ask.add_argument("--ollama-url", default=OLLAMA_DEFAULT_URL)
    ask.add_argument("--limit", type=int, default=5, help="FTS 적중 청크 수")
    ask.add_argument("--context", type=int, default=1, help="각 적중 앞뒤 청크 수")
    ask.add_argument("--max-context-chars", type=int, default=24000)
    ask.add_argument("--per-chunk-chars", type=int, default=6000)
    ask.add_argument("--num-ctx", type=int, default=16384)
    ask.add_argument("--timeout", type=int, default=300, help="Ollama 응답 제한 시간(초)")
    ask.set_defaults(func=command_ask)
    stats = sub.add_parser("stats", help="인덱스 요약")
    stats.add_argument("--db", required=True)
    stats.set_defaults(func=command_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "limit") and args.limit < 1:
        raise SystemExit("--limit은 1 이상이어야 합니다")
    if hasattr(args, "context") and args.context < 0:
        raise SystemExit("--context는 0 이상이어야 합니다")
    try:
        return args.func(args)
    except (OSError, zipfile.BadZipFile, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
