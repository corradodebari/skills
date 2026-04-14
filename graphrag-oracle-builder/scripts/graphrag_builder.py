#!/usr/bin/env python3
"""
graphrag_builder.py — GraphRAG ingestion pipeline for Oracle Property Graph

Steps:
  1. (Optional) Create a dedicated Oracle user/schema for the GraphRAG workload
  2. Chunk documents with Docling (via file_to_chunks.py)
  3. Extract entities and relationships via LLM (OpenAI or Ollama)
  4. Create or reuse an Oracle Property Graph schema
  5. Insert vertices (entities) and edges (relationships)
  6. Add vector embeddings to every vertex and edge

Usage — run the full pipeline:
  python graphrag_builder.py \
    --db-user graphuser --db-password mypass --db-dsn host:1521/FREEPDB1 \
    --graph-name MY_KG \
    doc1.pdf doc2.docx report.html

Usage — create a dedicated user first (requires a privileged / DBA connection):
  python graphrag_builder.py \
    --db-user sys --db-password syspass --db-dsn host:1521/FREEPDB1 \
    --create-user \
    --graph-name MY_KG \
    doc1.pdf

See SKILL.md for the full option reference.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import oracledb
import requests

# file_to_chunks.py must live in the same directory as this script
sys.path.insert(0, str(Path(__file__).parent))
from file_to_chunks import chunk_document


# ── User / schema setup ──────────────────────────────────────────────────────

def create_user_interactively(conn: oracledb.Connection) -> tuple[str, str]:
    """
    Interactively ask for a new Oracle username and password, confirm with the
    operator, then create the user and grant all privileges needed for GraphRAG
    workloads (property graphs, vectors, ONNX models, external network calls).

    Returns (username, password) so the caller can reconnect as the new user.
    """
    print("\n" + "=" * 60)
    print("CREATE ORACLE USER FOR GraphRAG")
    print("=" * 60)

    # Ask for username
    username = input("  New username [graphuser]: ").strip() or "graphuser"
    password = getpass.getpass(f"  Password for {username} [Welcome12345]: ").strip() or "Welcome12345"

    print(f"\n  The following user will be created: {username.upper()}")
    print("  Grants included:")
    print("    - CONNECT, RESOURCE, CREATE SESSION, DB_DEVELOPER_ROLE")
    print("    - CREATE PROPERTY GRAPH, CREATE TABLE, CREATE VIEW")
    print("    - CREATE SEQUENCE, CREATE PROCEDURE, CREATE MINING MODEL")
    print("    - EXECUTE on DBMS_VECTOR, DBMS_VECTOR_CHAIN, CTX_DDL")
    print("    - Network ACL: unrestricted outbound TCP (for Ollama / external LLMs)")

    confirm = input("\n  Proceed? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        sys.exit(0)

    uname_upper = username.upper()

    statements = [
        # === USER CREATION ===
        f"""CREATE USER {username}
  IDENTIFIED BY "{password}"
  DEFAULT TABLESPACE users
  TEMPORARY TABLESPACE temp
  QUOTA UNLIMITED ON users""",

        # === ROLES ===
        f"GRANT CONNECT, RESOURCE TO {username}",
        f"GRANT CREATE SESSION TO {username}",
        f"GRANT DB_DEVELOPER_ROLE TO {username}",

        # === PROPERTY GRAPH PRIVILEGES ===
        f"GRANT CREATE PROPERTY GRAPH TO {username}",

        # === DATABASE OBJECT PRIVILEGES ===
        f"GRANT CREATE TABLE TO {username}",
        f"GRANT CREATE VIEW TO {username}",
        f"GRANT CREATE SEQUENCE TO {username}",
        f"GRANT CREATE PROCEDURE TO {username}",

        # === AI / VECTOR / EMBEDDING PRIVILEGES ===
        f"GRANT CREATE MINING MODEL TO {username}",          # ONNX models in DB
        f"GRANT EXECUTE ON DBMS_VECTOR TO {username}",       # vector search + embedding
        f"GRANT EXECUTE ON DBMS_VECTOR_CHAIN TO {username}", # chunking, summarization
        f"GRANT EXECUTE ON CTX_DDL TO {username}",           # Oracle Text (full-text indexing)
    ]

    # Network ACL: allow outbound TCP to any host (needed for external LLMs / Ollama)
    network_acl_block = f"""BEGIN
  DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
    host => '*',
    ace  => xs$ace_type(
              privilege_list => xs$name_list('connect'),
              principal_name => '{uname_upper}',
              principal_type => xs_acl.ptype_db
            )
  );
END;"""

    with conn.cursor() as cur:
        for stmt in statements:
            print(f"  Executing: {stmt.splitlines()[0]} …")
            cur.execute(stmt)
        print("  Executing: DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE …")
        cur.execute(network_acl_block)

    conn.commit()
    print(f"\n  User {uname_upper} created successfully.\n")
    return username, password


# ── LLM / embedding helpers ───────────────────────────────────────────────────

def _ollama_generate(prompt: str, model: str, base_url: str) -> str:
    resp = requests.post(
        f"{base_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            # Force valid JSON responses for downstream schema parsing.
            "format": "json",
            "options": {"temperature": 0},
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def _ollama_list_models(base_url: str) -> list[str]:
    resp = requests.get(f"{base_url}/api/tags", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [m["name"] for m in data.get("models", []) if m.get("name")]


def _prompt_ollama_model_required(
    kind: str,
    models: list[str],
    cli_value: str | None = None,
) -> str:
    """Select an Ollama model with no implicit default."""
    if cli_value:
        if cli_value in models:
            print(f"  Using {kind} model from CLI: {cli_value}")
            return cli_value
        raise RuntimeError(
            f"{kind} model '{cli_value}' not found in Ollama local models."
        )
    while True:
        try:
            raw = input(f"  {kind} model (required, number or name): ").strip()
        except EOFError:
            raise RuntimeError(
                f"{kind} model selection is required (no implicit Ollama default)."
            )
        if not raw:
            print("    Selection is required.")
            continue
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(models):
                return models[idx - 1]
        if raw in models:
            return raw
        print("    Invalid selection. Enter model name or list number.")


def _openai_generate(prompt: str, model: str, api_key: str, base_url: str) -> str:
    resp = requests.post(
        f"{base_url.rstrip('/')}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": prompt,
        },
        timeout=600,
    )
    resp.raise_for_status()
    data = resp.json()

    output_text = data.get("output_text")
    if output_text:
        return output_text

    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    if parts:
        return "\n".join(parts)

    raise RuntimeError("OpenAI response did not include text output")


def _prompt_openai_model_required(kind: str, cli_value: str | None = None) -> str:
    """Select an OpenAI model with no implicit default."""
    if cli_value and cli_value.strip():
        print(f"  Using {kind} model from CLI: {cli_value.strip()}")
        return cli_value.strip()
    while True:
        try:
            raw = input(f"  {kind} model (required, name): ").strip()
        except EOFError:
            raise RuntimeError(
                f"{kind} model selection is required (no implicit OpenAI default)."
            )
        if raw:
            return raw
        print("    Selection is required.")


def _ollama_embed(text: str, model: str, base_url: str) -> list[float]:
    resp = requests.post(
        f"{base_url}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _openai_embed(text: str, model: str, api_key: str, base_url: str) -> list[float]:
    resp = requests.post(
        f"{base_url.rstrip('/')}/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": text,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", [])
    if not items or "embedding" not in items[0]:
        raise RuntimeError("OpenAI embeddings response did not include data[0].embedding")
    return items[0]["embedding"]


def _oracle_embed(conn: oracledb.Connection, text: str, model_name: str) -> list[float]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DBMS_VECTOR.UTL_TO_EMBEDDING(:txt, json(:cfg)) FROM dual",
            txt=text,
            cfg=json.dumps({"provider": "database", "model": model_name}),
        )
        row = cur.fetchone()
    return list(row[0])


# ── Graph schema extraction ───────────────────────────────────────────────────

def extract_graph_schema(
    chunks: list[str],
    prompt_template: str,
    llm_provider: str,
    llm_model: str,
    ollama_url: str,
    openai_api_key: str | None = None,
    openai_base_url: str = "https://api.openai.com/v1",
) -> dict:
    """Call the LLM with the entity-extraction prompt and return the parsed JSON schema."""
    chunks_text = "\n\n---\n\n".join(chunks)
    prompt = prompt_template.replace("{{INSERT_TEXT_CHUNKS_HERE}}", chunks_text)
    print(f"  Sending {len(chunks)} chunks to LLM ({llm_provider}:{llm_model}) …")

    if llm_provider == "ollama":
        raw = _ollama_generate(prompt, llm_model, ollama_url).strip()
    elif llm_provider == "openai":
        if not openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when --llm-provider openai is used."
            )
        raw = _openai_generate(prompt, llm_model, openai_api_key, openai_base_url).strip()
    else:
        raise RuntimeError(f"Unsupported llm provider: {llm_provider}")

    # Strip markdown code fences if the model added them
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:])
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3].rstrip()
    return json.loads(raw)


def write_chunks_json(
    reference_docs: list[str],
    chunk_entries: list[dict[str, object]],
    output_path: Path = Path("temp/output_chunks.json"),
) -> None:
    """Persist raw chunk text and source file references for audit/debug."""
    normalized_chunk_entries: list[dict[str, object]] = []
    for entry in chunk_entries:
        text = str(entry.get("text", ""))
        metadata = entry.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        ref = metadata.get("ref") or entry.get("ref") or ""
        uid = metadata.get("uuid") or entry.get("uuid") or str(uuid4())

        normalized_chunk_entries.append(
            {
                "text": text,
                "metadata": {
                    "ref": str(ref),
                    "uuid": str(uid),
                },
            }
        )

    payload = {
        "reference_docs": reference_docs,
        "chunks": normalized_chunk_entries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved chunk list JSON: {output_path.resolve()}")


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _normalize_uuid_values(raw) -> list[str]:
    """Return a clean, de-duplicated UUID list from arbitrary input."""
    if not isinstance(raw, list):
        return []
    cleaned = [str(item).strip() for item in raw]
    cleaned = [item for item in cleaned if item]
    return _ordered_unique(cleaned)


def _uuids_json_string(raw) -> str:
    """Serialize UUID list to compact JSON text for storage in table rows."""
    return json.dumps(_normalize_uuid_values(raw), ensure_ascii=False)


def enrich_schema_with_chunk_uuids(schema: dict, chunks_payload: dict) -> dict:
    """
    Ensure extraction schema contains UUID provenance:
      - vertex[].properties[].uuids
      - connection[].uuids

    Label-level UUIDs are explicitly removed:
      - vertex[].uuids
      - edge[].uuids
    """
    chunks = chunks_payload.get("chunks", [])
    if not isinstance(chunks, list):
        return schema

    normalized_chunks: list[tuple[str, str]] = []
    known_uuids: set[str] = set()
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text", ""))
        metadata = chunk.get("metadata")
        if not isinstance(metadata, dict):
            continue
        uuid = str(metadata.get("uuid", "")).strip()
        if not uuid:
            continue
        normalized_chunks.append((uuid, text.lower()))
        known_uuids.add(uuid)

    def _normalize_uuid_list(raw) -> list[str]:
        if not isinstance(raw, list):
            return []
        filtered = [str(item).strip() for item in raw]
        filtered = [item for item in filtered if item in known_uuids]
        return _ordered_unique(filtered)

    vertex_by_entity_name: dict[str, dict] = {}
    for vertex in schema.get("vertex", []):
        if not isinstance(vertex, dict):
            continue
        properties = vertex.get("properties", [])
        if not isinstance(properties, list):
            continue
        for prop in properties:
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name", "")).strip()
            if not name:
                continue
            existing = _normalize_uuid_list(prop.get("uuids"))
            inferred: list[str] = []
            needle = name.lower()
            for uuid, chunk_text in normalized_chunks:
                if needle and needle in chunk_text:
                    inferred.append(uuid)
            merged = _ordered_unique(existing + inferred)
            prop["uuids"] = merged
            vertex_by_entity_name[name] = prop

        # Enforce "no label-level UUIDs" on vertex objects.
        vertex.pop("uuids", None)

    edge_lookup: dict[str, dict] = {}
    for edge in schema.get("edge", []):
        if not isinstance(edge, dict):
            continue
        label = str(edge.get("label", "")).strip()
        if not label:
            continue
        # Enforce "no label-level UUIDs" on edge objects.
        edge.pop("uuids", None)
        edge_lookup[label] = edge

    for connection in schema.get("connection", []):
        triple = _connection_triple(connection)
        if not triple:
            continue
        src_name, edge_label, dst_name = [str(x).strip() for x in triple]
        edge_entry = edge_lookup.get(edge_label)
        if not edge_entry:
            continue

        inferred: list[str] = []
        src_lower = src_name.lower()
        dst_lower = dst_name.lower()
        for uuid, chunk_text in normalized_chunks:
            if src_lower and dst_lower and src_lower in chunk_text and dst_lower in chunk_text:
                inferred.append(uuid)

        src_prop = vertex_by_entity_name.get(src_name)
        dst_prop = vertex_by_entity_name.get(dst_name)
        if src_prop:
            inferred.extend(_normalize_uuid_list(src_prop.get("uuids")))
        if dst_prop:
            inferred.extend(_normalize_uuid_list(dst_prop.get("uuids")))

        if isinstance(connection, dict):
            existing_conn_uuids = _normalize_uuid_list(connection.get("uuids"))
            connection["uuids"] = _ordered_unique(existing_conn_uuids + inferred)

    return schema


def _connection_triple(connection) -> tuple[str, str, str] | None:
    """
    Accept both connection formats:
      1) ["src", "EDGE", "dst"]
      2) {"triple": ["src", "EDGE", "dst"], "uuids": [...]}
    """
    if isinstance(connection, (list, tuple)) and len(connection) == 3:
        return (str(connection[0]), str(connection[1]), str(connection[2]))
    if isinstance(connection, dict):
        triple = connection.get("triple")
        if isinstance(triple, (list, tuple)) and len(triple) == 3:
            return (str(triple[0]), str(triple[1]), str(triple[2]))
    return None


# ── Oracle helpers ────────────────────────────────────────────────────────────

def _table_exists(conn: oracledb.Connection, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = UPPER(:1)", [name]
        )
        return cur.fetchone()[0] > 0


def _graph_exists(conn: oracledb.Connection, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM user_property_graphs WHERE graph_name = UPPER(:1)",
            [name],
        )
        return cur.fetchone()[0] > 0


def _pgql_label(label: str) -> str:
    """Quote PGQL labels so reserved words are always valid."""
    return '"' + label.replace('"', '""') + '"'


def _find_label_for_entity(schema: dict, entity_name: str) -> str | None:
    """Return the vertex label whose properties list contains entity_name."""
    for vertex in schema["vertex"]:
        for prop in vertex.get("properties", []):
            if prop["name"] == entity_name:
                return vertex["label"]
    return None


def _vertex_table_name(label: str) -> str:
    """Vertex table naming for generated SQL scripts."""
    return f"V_{label.lower()}"


def _edge_table_name(label: str) -> str:
    """Edge table naming for generated SQL scripts."""
    return f"E_{label.lower()}"


# ── Schema creation ───────────────────────────────────────────────────────────

def _create_vertex_tables(conn: oracledb.Connection, schema: dict, emb_dim: int):
    with conn.cursor() as cur:
        for vertex in schema["vertex"]:
            tname = f"v_{vertex['label'].lower()}"
            if _table_exists(conn, tname):
                print(f"    [skip] {tname} already exists")
                continue
            cur.execute(f"""
                CREATE TABLE {tname} (
                  v_id       NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  name       VARCHAR2(1000) NOT NULL,
                  source_doc VARCHAR2(4000),
                  uuids      CLOB,
                  embedding  VECTOR({emb_dim}, FLOAT32)
                )
            """)
            print(f"    Created vertex table: {tname}")
    conn.commit()


def _build_edge_connections(schema: dict) -> dict[str, tuple[str, str]]:
    """Return {edge_label: (src_vertex_label, dst_vertex_label)} from connection triples.

    Handles two LLM output styles:
      - instance names  ("Dr. Elena Vasquez", "FOUNDED", "NovaTech Solutions")
      - vertex labels   ("Person", "FOUNDED", "Organization")
    """
    vertex_labels = {v["label"] for v in schema["vertex"]}
    result: dict[str, tuple[str, str]] = {}
    for connection in schema.get("connection", []):
        # Some LLM responses may emit malformed connection entries.
        # Skip invalid records instead of failing the entire pipeline.
        triple = _connection_triple(connection)
        if not triple:
            print(f"    [warn] Invalid connection entry (expected triple): {connection!r}")
            continue
        src_name, edge_label, dst_name = triple
        if edge_label in result:
            continue
        # Primary: entity instance name lookup
        src_label = _find_label_for_entity(schema, src_name)
        dst_label = _find_label_for_entity(schema, dst_name)
        # Fallback: the LLM used vertex labels directly in the triple
        if not src_label and src_name in vertex_labels:
            src_label = src_name
        if not dst_label and dst_name in vertex_labels:
            dst_label = dst_name
        if src_label and dst_label:
            result[edge_label] = (src_label, dst_label)
    return result


def _create_edge_tables(
    conn: oracledb.Connection,
    schema: dict,
    edge_connections: dict[str, tuple[str, str]],
    emb_dim: int,
):
    with conn.cursor() as cur:
        for edge in schema["edge"]:
            elabel = edge["label"]
            tname = f"e_{elabel.lower()}"
            if _table_exists(conn, tname):
                print(f"    [skip] {tname} already exists")
                continue
            pair = edge_connections.get(elabel)
            if not pair:
                print(f"    [warn] No connections found for edge {elabel} — skipping table")
                continue
            src_vtable = f"v_{pair[0].lower()}"
            dst_vtable = f"v_{pair[1].lower()}"
            cur.execute(f"""
                CREATE TABLE {tname} (
                  edge_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  src_v_id    NUMBER NOT NULL REFERENCES {src_vtable}(v_id),
                  dst_v_id    NUMBER NOT NULL REFERENCES {dst_vtable}(v_id),
                  description VARCHAR2(4000),
                  uuids       CLOB,
                  embedding   VECTOR({emb_dim}, FLOAT32)
                )
            """)
            print(f"    Created edge table: {tname}")
    conn.commit()


def _create_property_graph(
    conn: oracledb.Connection,
    schema: dict,
    graph_name: str,
    edge_connections: dict[str, tuple[str, str]],
):
    if _graph_exists(conn, graph_name):
        print(f"    Property graph {graph_name} already exists — skipping DDL")
        return

    vertex_clauses = []
    for v in schema["vertex"]:
        tname = f"v_{v['label'].lower()}"
        vertex_clauses.append(
            f"    {tname}\n"
            f"      KEY (v_id)\n"
            f"      LABEL {_pgql_label(v['label'])}\n"
            f"      PROPERTIES (name, source_doc, uuids, embedding)"
        )

    edge_clauses = []
    for e in schema["edge"]:
        elabel = e["label"]
        pair = edge_connections.get(elabel)
        if not pair:
            continue
        tname = f"e_{elabel.lower()}"
        src_vtable = f"v_{pair[0].lower()}"
        dst_vtable = f"v_{pair[1].lower()}"
        edge_clauses.append(
            f"    {tname}\n"
            f"      KEY (edge_id)\n"
            f"      SOURCE KEY (src_v_id) REFERENCES {src_vtable} (v_id)\n"
            f"      DESTINATION KEY (dst_v_id) REFERENCES {dst_vtable} (v_id)\n"
            f"      LABEL {_pgql_label(elabel)}\n"
            f"      PROPERTIES (description, uuids, embedding)"
        )

    ddl = (
        f"CREATE PROPERTY GRAPH {graph_name}\n"
        f"  VERTEX TABLES (\n" + ",\n".join(vertex_clauses) + "\n  )"
    )
    if edge_clauses:
        ddl += "\n  EDGE TABLES (\n" + ",\n".join(edge_clauses) + "\n  )"

    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    print(f"    Property graph {graph_name} created.")


def drop_schema(conn: oracledb.Connection, schema: dict, graph_name: str):
    with conn.cursor() as cur:
        try:
            cur.execute(f"DROP PROPERTY GRAPH {graph_name}")
        except Exception:
            pass

        # Drop all GraphRAG-style tables to avoid stale objects when label sets
        # differ across extractions.
        cur.execute(
            "SELECT table_name FROM user_tables "
            "WHERE REGEXP_LIKE(table_name, '^(E|V)_') "
            "ORDER BY CASE WHEN table_name LIKE 'E_%' THEN 1 ELSE 2 END"
        )
        for (table_name,) in cur.fetchall():
            try:
                cur.execute(f"DROP TABLE {table_name} CASCADE CONSTRAINTS PURGE")
            except Exception:
                pass
    conn.commit()


def create_schema(
    conn: oracledb.Connection,
    schema: dict,
    graph_name: str,
    emb_dim: int,
):
    edge_connections = _build_edge_connections(schema)
    _create_vertex_tables(conn, schema, emb_dim)
    _create_edge_tables(conn, schema, edge_connections, emb_dim)
    _create_property_graph(conn, schema, graph_name, edge_connections)
    return edge_connections


# ── SQL script generation (SQLcl / --sql-output mode) ────────────────────────

def _vec_sql(vec: list[float], dim: int) -> str:
    """Format a float list as an Oracle TO_VECTOR(...) call."""
    values = ",".join(f"{v:.8f}" for v in vec)
    return f"TO_VECTOR('[{values}]', {dim}, FLOAT32)"


def _user_creation_sql(username: str, password: str) -> list[str]:
    """Return the DDL/GRANT statements to create a GraphRAG-ready Oracle user."""
    u = username
    U = username.upper()
    return [
        # === USER CREATION ===
        f"""CREATE USER {u}
  IDENTIFIED BY "{password}"
  DEFAULT TABLESPACE users
  TEMPORARY TABLESPACE temp
  QUOTA UNLIMITED ON users""",

        # === ROLES ===
        f"GRANT CONNECT, RESOURCE TO {u}",
        f"GRANT CREATE SESSION TO {u}",
        f"GRANT DB_DEVELOPER_ROLE TO {u}",

        # === PROPERTY GRAPH PRIVILEGES ===
        f"GRANT CREATE PROPERTY GRAPH TO {u}",

        # === DATABASE OBJECT PRIVILEGES ===
        f"GRANT CREATE TABLE TO {u}",
        f"GRANT CREATE VIEW TO {u}",
        f"GRANT CREATE SEQUENCE TO {u}",
        f"GRANT CREATE PROCEDURE TO {u}",

        # === AI / VECTOR / EMBEDDING PRIVILEGES ===
        f"GRANT CREATE MINING MODEL TO {u}",           # ONNX models in DB
        f"GRANT EXECUTE ON DBMS_VECTOR TO {u}",        # vector search + embedding
        f"GRANT EXECUTE ON DBMS_VECTOR_CHAIN TO {u}",  # chunking, summarization
        f"GRANT EXECUTE ON CTX_DDL TO {u}",            # Oracle Text (full-text indexing)

        # === NETWORK ACL (for external LLM / Ollama calls) ===
        f"""BEGIN
  DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
    host => '*',
    ace  => xs$ace_type(
              privilege_list => xs$name_list('connect'),
              principal_name => '{U}',
              principal_type => xs_acl.ptype_db
            )
  );
END;""",
    ]


def generate_sql_script(
    schema: dict,
    graph_name: str,
    source_docs_str: str,
    embed_fn,
    emb_dim: int,
    create_user_info: tuple[str, str] | None = None,
    force_recreate: bool = False,
) -> str:
    """
    Build a self-contained SQL script with DDL + DML (including TO_VECTOR embeddings).
    The result can be executed via SQLcl or saved to a .sql file.

    Edge INSERTs use correlated sub-SELECTs so v_id values do not need to be known
    at generation time:
        INSERT INTO MY_KG_E_works_at (src_v_id, dst_v_id, ...)
        SELECT (SELECT v_id FROM MY_KG_V_person WHERE name = '...'),
               (SELECT v_id FROM MY_KG_V_company WHERE name = '...'),
               ...
        FROM dual;
    """
    lines: list[str] = []
    graph_prefix = graph_name.upper()

    def vertex_table_name(label: str) -> str:
        return f"{graph_prefix}_V_{label.lower()}"

    def edge_table_name(label: str) -> str:
        return f"{graph_prefix}_E_{label.lower()}"

    def section(title: str):
        lines.extend(["", f"-- {'=' * 58}", f"-- {title}", f"-- {'=' * 58}"])

    lines.append(f"-- GraphRAG Oracle Builder — Generated SQL")
    lines.append(f"-- Graph   : {graph_name}")
    lines.append(f"-- Sources : {source_docs_str}")
    lines.append("-- MANDATORY: DO NOT APPLY THIS *_setup.sql TO A DATABASE WITHOUT USER CONFIRMATION.")
    lines.append("")

    # ── Optional: user creation (must be run as DBA) ──────────────────────────
    if create_user_info:
        username, password = create_user_info
        section("USER CREATION — run this block as DBA / SYSDBA")
        lines.append(f"-- After this block, reconnect as {username.upper()} to run the rest.")
        lines.append("")
        for stmt in _user_creation_sql(username, password):
            lines.append(stmt + ";")
            lines.append("")
        lines.append("COMMIT;")

    # ── Optional: drop existing objects ──────────────────────────────────────
    if force_recreate:
        section("DROP EXISTING OBJECTS (force-recreate)")
        lines.extend(
            [
                "BEGIN",
                f"  BEGIN EXECUTE IMMEDIATE 'DROP PROPERTY GRAPH {graph_name}'; EXCEPTION WHEN OTHERS THEN NULL; END;",
                "  FOR r IN (",
                "    SELECT table_name",
                "    FROM user_tables",
                f"    WHERE table_name LIKE '{graph_prefix}_E_%'",
                f"       OR table_name LIKE '{graph_prefix}_V_%'",
                f"    ORDER BY CASE WHEN table_name LIKE '{graph_prefix}_E_%' THEN 1 ELSE 2 END",
                "  ) LOOP",
                "    BEGIN",
                "      EXECUTE IMMEDIATE 'DROP TABLE ' || r.table_name || ' CASCADE CONSTRAINTS PURGE';",
                "    EXCEPTION WHEN OTHERS THEN NULL;",
                "    END;",
                "  END LOOP;",
                "END;",
                "/",
            ]
        )

    # ── Vertex tables DDL ─────────────────────────────────────────────────────
    section("VERTEX TABLES")
    for vertex in schema["vertex"]:
        tname = vertex_table_name(vertex["label"])
        lines.append(f"CREATE TABLE {tname} (")
        lines.append(f"  v_id       NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,")
        lines.append(f"  name       VARCHAR2(1000) NOT NULL,")
        lines.append(f"  source_doc VARCHAR2(4000),")
        lines.append(f"  uuids      CLOB,")
        lines.append(f"  embedding  VECTOR({emb_dim}, FLOAT32)")
        lines.append(f");")
        lines.append("")

    # ── Edge tables DDL ───────────────────────────────────────────────────────
    edge_connections = _build_edge_connections(schema)
    section("EDGE TABLES")
    for edge in schema["edge"]:
        elabel = edge["label"]
        tname = edge_table_name(elabel)
        pair = edge_connections.get(elabel)
        if not pair:
            lines.append(f"-- [warn] No connections found for edge {elabel} — skipped")
            continue
        src_vtable = vertex_table_name(pair[0])
        dst_vtable = vertex_table_name(pair[1])
        lines.append(f"CREATE TABLE {tname} (")
        lines.append(f"  edge_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,")
        lines.append(f"  src_v_id    NUMBER NOT NULL REFERENCES {src_vtable}(v_id),")
        lines.append(f"  dst_v_id    NUMBER NOT NULL REFERENCES {dst_vtable}(v_id),")
        lines.append(f"  description VARCHAR2(4000),")
        lines.append(f"  uuids       CLOB,")
        lines.append(f"  embedding   VECTOR({emb_dim}, FLOAT32)")
        lines.append(f");")
        lines.append("")

    # ── Property Graph DDL ────────────────────────────────────────────────────
    section("PROPERTY GRAPH DDL")
    vertex_clauses = []
    for v in schema["vertex"]:
        tname = vertex_table_name(v["label"])
        vertex_clauses.append(
            f"    {tname}\n      KEY (v_id)\n"
            f"      LABEL {_pgql_label(v['label'])}\n"
            f"      PROPERTIES (name, source_doc, uuids, embedding)"
        )
    edge_clauses = []
    for e in schema["edge"]:
        elabel = e["label"]
        pair = edge_connections.get(elabel)
        if not pair:
            continue
        tname = edge_table_name(elabel)
        src_vtable = vertex_table_name(pair[0])
        dst_vtable = vertex_table_name(pair[1])
        edge_clauses.append(
            f"    {tname}\n      KEY (edge_id)\n"
            f"      SOURCE KEY (src_v_id) REFERENCES {src_vtable} (v_id)\n"
            f"      DESTINATION KEY (dst_v_id) REFERENCES {dst_vtable} (v_id)\n"
            f"      LABEL {_pgql_label(elabel)}\n"
            f"      PROPERTIES (description, uuids, embedding)"
        )
    graph_ddl = (
        f"CREATE PROPERTY GRAPH {graph_name}\n"
        f"  VERTEX TABLES (\n" + ",\n".join(vertex_clauses) + "\n  )"
    )
    if edge_clauses:
        graph_ddl += "\n  EDGE TABLES (\n" + ",\n".join(edge_clauses) + "\n  )"
    lines.append(graph_ddl + ";")

    # ── Vertex INSERT statements ──────────────────────────────────────────────
    section("INSERT VERTICES (with embeddings)")
    seen: set[tuple[str, str]] = set()
    for vertex in schema["vertex"]:
        tname = vertex_table_name(vertex["label"])
        for prop in vertex.get("properties", []):
            name = prop["name"]
            seen_key = (tname, name)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            print(f"  Embedding [{vertex['label']}] {name} …")
            vec = embed_fn(name)
            safe_name = name.replace("'", "''")
            safe_src = source_docs_str.replace("'", "''")
            uuids_json = _uuids_json_string(prop.get("uuids") or vertex.get("uuids"))
            safe_uuids = uuids_json.replace("'", "''")
            lines.append(
                f"INSERT INTO {tname} (name, source_doc, uuids, embedding) VALUES ("
                f"'{safe_name}', '{safe_src}', '{safe_uuids}', {_vec_sql(vec, emb_dim)});"
            )
    lines.append("")
    lines.append("COMMIT;")

    # ── Edge INSERT statements ────────────────────────────────────────────────
    section("INSERT EDGES (with embeddings)")
    edge_uuid_fallback: dict[str, list[str]] = {
        str(edge.get("label", "")): _normalize_uuid_values(edge.get("uuids"))
        for edge in schema.get("edge", [])
        if isinstance(edge, dict)
    }
    for connection in schema.get("connection", []):
        triple = _connection_triple(connection)
        if not triple:
            lines.append(f"-- [warn] Skipping invalid connection entry: {connection!r}")
            continue
        src_name, edge_label, dst_name = triple
        tname = edge_table_name(edge_label)
        pair = edge_connections.get(edge_label)
        if not pair:
            lines.append(f"-- [warn] Skipping {edge_label}: no connection info")
            continue
        src_vtable = vertex_table_name(pair[0])
        dst_vtable = vertex_table_name(pair[1])
        description = f"{src_name} {edge_label.replace('_', ' ').lower()} {dst_name}"
        print(f"  Embedding [{edge_label}] {src_name} → {dst_name} …")
        vec = embed_fn(description)
        safe_src = src_name.replace("'", "''")
        safe_dst = dst_name.replace("'", "''")
        safe_desc = description.replace("'", "''")
        conn_uuids = []
        if isinstance(connection, dict):
            conn_uuids = _normalize_uuid_values(connection.get("uuids"))
        if not conn_uuids:
            conn_uuids = edge_uuid_fallback.get(edge_label, [])
        safe_uuids = _uuids_json_string(conn_uuids).replace("'", "''")
        lines.append(
            f"INSERT INTO {tname} (src_v_id, dst_v_id, description, uuids, embedding)\n"
            f"  SELECT (SELECT MIN(v_id) FROM {src_vtable} WHERE name = '{safe_src}'),\n"
            f"         (SELECT MIN(v_id) FROM {dst_vtable} WHERE name = '{safe_dst}'),\n"
            f"         '{safe_desc}',\n"
            f"         '{safe_uuids}',\n"
            f"         {_vec_sql(vec, emb_dim)}\n"
            f"  FROM dual\n"
            f"  WHERE EXISTS (SELECT 1 FROM {src_vtable} WHERE name = '{safe_src}')\n"
            f"    AND EXISTS (SELECT 1 FROM {dst_vtable} WHERE name = '{safe_dst}');"
        )
    lines.append("")
    lines.append("COMMIT;")

    return "\n".join(lines)


# ── Data insertion ────────────────────────────────────────────────────────────

def insert_entities(
    conn: oracledb.Connection,
    schema: dict,
    source_docs: str,
    embed_fn,
    emb_dim: int,
) -> dict[tuple[str, str], int]:
    """Insert entity instances as vertex rows. Returns {(label, name): v_id}."""
    entity_ids: dict[tuple[str, str], int] = {}
    with conn.cursor() as cur:
        for vertex in schema["vertex"]:
            label = vertex["label"]
            tname = f"v_{label.lower()}"
            for prop in vertex.get("properties", []):
                name = prop["name"]
                key = (label, name)
                if key in entity_ids:
                    continue
                print(f"  [{label}] {name}")
                embedding = embed_fn(name)
                vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
                uuids_json = _uuids_json_string(prop.get("uuids") or vertex.get("uuids"))
                out_var = cur.var(oracledb.DB_TYPE_NUMBER)
                cur.execute(
                    f"INSERT INTO {tname} (name, source_doc, uuids, embedding) "
                    f"VALUES (:name, :src, :uuids, TO_VECTOR(:emb, {emb_dim}, FLOAT32)) RETURNING v_id INTO :vid",
                    name=name,
                    src=source_docs,
                    uuids=uuids_json,
                    emb=vec_str,
                    vid=out_var,
                )
                entity_ids[key] = int(out_var.getvalue()[0])
    conn.commit()
    return entity_ids


def insert_relationships(
    conn: oracledb.Connection,
    schema: dict,
    entity_ids: dict[tuple[str, str], int],
    edge_connections: dict[str, tuple[str, str]],
    embed_fn,
    emb_dim: int,
):
    """Insert connection triples as edge rows with embeddings."""
    edge_uuid_fallback: dict[str, list[str]] = {
        str(edge.get("label", "")): _normalize_uuid_values(edge.get("uuids"))
        for edge in schema.get("edge", [])
        if isinstance(edge, dict)
    }
    with conn.cursor() as cur:
        for connection in schema.get("connection", []):
            triple = _connection_triple(connection)
            if not triple:
                print(f"  [warn] Skipping invalid connection entry: {connection!r}")
                continue
            src_name, edge_label, dst_name = triple
            tname = f"e_{edge_label.lower()}"
            pair = edge_connections.get(edge_label)
            if not pair:
                print(f"  [warn] Skipping {src_name} --{edge_label}--> {dst_name}: no edge mapping")
                continue
            src_label, dst_label = pair
            src_id = entity_ids.get((src_label, src_name))
            dst_id = entity_ids.get((dst_label, dst_name))
            if src_id is None or dst_id is None:
                print(f"  [warn] Skipping {src_name} --{edge_label}--> {dst_name}: entity not found")
                continue
            description = f"{src_name} {edge_label.replace('_', ' ').lower()} {dst_name}"
            embedding = embed_fn(description)
            vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
            conn_uuids = []
            if isinstance(connection, dict):
                conn_uuids = _normalize_uuid_values(connection.get("uuids"))
            if not conn_uuids:
                conn_uuids = edge_uuid_fallback.get(edge_label, [])
            uuids_json = _uuids_json_string(conn_uuids)
            print(f"  [{edge_label}] {src_name} → {dst_name}")
            cur.execute(
                f"INSERT INTO {tname} (src_v_id, dst_v_id, description, uuids, embedding) "
                f"VALUES (:src, :dst, :descr, :uuids, TO_VECTOR(:emb, {emb_dim}, FLOAT32))",
                src=src_id,
                dst=dst_id,
                descr=description,
                uuids=uuids_json,
                emb=vec_str,
            )
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="GraphRAG Oracle Builder — chunk docs, extract graph, populate Oracle Property Graph + embeddings"
    )
    parser.add_argument("documents", nargs="+", type=Path, help="Input documents (Docling-supported formats)")
    parser.add_argument("--db-user", default=None, help="Oracle username (not needed with --sql-output)")
    parser.add_argument("--db-password", default=None, help="Oracle password (not needed with --sql-output)")
    parser.add_argument("--db-dsn", default=None, help="Oracle DSN: host:port/service_name (not needed with --sql-output)")
    parser.add_argument("--graph-name", required=True, help="Oracle Property Graph name (uppercase recommended)")
    parser.add_argument(
        "--llm-provider",
        choices=["ollama", "openai"],
        default="openai",
        help="LLM provider for entity/relation extraction",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Ollama LLM model for extraction (optional; otherwise selected interactively from local Ollama models)",
    )
    parser.add_argument(
        "--embed-provider",
        choices=["ollama", "openai"],
        default="ollama",
        help="Embedding provider for vertex/edge embeddings (use --oracle-embed for Oracle DBMS_VECTOR)",
    )
    parser.add_argument(
        "--embed-model",
        default=None,
        help="Ollama embedding model (required when --embed-provider ollama unless provided interactively)",
    )
    parser.add_argument(
        "--openai-embed-model",
        default=None,
        help="OpenAI embedding model (required when --embed-provider openai unless provided interactively)",
    )
    parser.add_argument("--embed-dim", type=int, default=768, help="Embedding dimension (must match model)")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    parser.add_argument(
        "--openai-model",
        default="gpt-5.3-codex",
        help="OpenAI model for extraction when --llm-provider openai",
    )
    parser.add_argument(
        "--openai-base-url",
        default="https://api.openai.com/v1",
        help="OpenAI API base URL",
    )
    parser.add_argument("--oracle-embed", action="store_true", help="Use Oracle DBMS_VECTOR for embeddings")
    parser.add_argument("--oracle-embed-model", default="doc_model", help="ONNX model name loaded in Oracle DB")
    parser.add_argument("--prompt", type=Path, default=None, help="Custom prompt template (default: bundled)")
    parser.add_argument("--force-recreate", action="store_true", help="Drop and recreate the graph schema")
    parser.add_argument(
        "--create-user",
        action="store_true",
        help=(
            "Interactively create a new Oracle user with all GraphRAG grants before running "
            "the pipeline. The --db-user/--db-password connection must have DBA privileges."
        ),
    )
    parser.add_argument(
        "--sql-output",
        metavar="FILE",
        default=None,
        help=(
            "Generate a self-contained SQL script (DDL + embeddings) and write it to FILE "
            "instead of connecting to Oracle directly. Use with SQLcl MCP: Claude executes "
            "the file via an existing SQLcl connection. No --db-* flags required in this mode."
        ),
    )
    args = parser.parse_args()
    llm_model = args.openai_model if args.llm_provider == "openai" else args.llm_model
    effective_embed_provider = "oracle" if args.oracle_embed else args.embed_provider
    embed_model = args.embed_model
    openai_embed_model = args.openai_embed_model

    openai_api_key = (
        os.getenv("OPENAI_API_KEY")
        if (args.llm_provider == "openai" or effective_embed_provider == "openai")
        else None
    )

    needs_ollama_embedding_model = effective_embed_provider == "ollama"
    if args.llm_provider == "ollama" or needs_ollama_embedding_model:
        try:
            available_models = _ollama_list_models(args.ollama_url)
        except Exception as exc:
            raise RuntimeError(
                f"Could not fetch Ollama model list from {args.ollama_url}: {exc}"
            ) from exc

        if not available_models:
            raise RuntimeError(
                "No Ollama models discovered. Run scripts/list_ollama_models.py to verify local models."
            )

        print("\nOllama models available:")
        for idx, model_name in enumerate(available_models, start=1):
            print(f"  {idx}. {model_name}")

        if args.llm_provider == "ollama":
            llm_model = _prompt_ollama_model_required(
                "Graph generation LLM", available_models, args.llm_model
            )
        if needs_ollama_embedding_model:
            embed_model = _prompt_ollama_model_required(
                "Embedding", available_models, args.embed_model
            )
    if effective_embed_provider == "openai":
        if not openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when --embed-provider openai is used."
            )
        openai_embed_model = _prompt_openai_model_required(
            "OpenAI embedding", args.openai_embed_model
        )

    # In SQL-output mode, db credentials are not required
    if not args.sql_output:
        missing = [f for f in ("db_user", "db_password", "db_dsn") if not getattr(args, f)]
        if missing:
            parser.error(
                f"The following arguments are required when not using --sql-output: "
                + ", ".join(f"--{f.replace('_','-')}" for f in missing)
            )

    # Load prompt template
    prompt_path = args.prompt or (Path(__file__).parent / "prompt_text_to_graph.txt")
    prompt_template = prompt_path.read_text(encoding="utf-8")

    # ══════════════════════════════════════════════════════════════════════════
    # SQL-OUTPUT MODE: generate a .sql file for execution via SQLcl MCP
    # ══════════════════════════════════════════════════════════════════════════
    if args.sql_output:
        sql_path = Path(args.sql_output)
        if effective_embed_provider == "oracle":
            raise RuntimeError(
                "--oracle-embed is not supported with --sql-output mode. "
                "Use --embed-provider ollama/openai for SQL generation."
            )
        if effective_embed_provider == "openai":
            embed_fn = lambda text: _openai_embed(
                text, openai_embed_model, openai_api_key, args.openai_base_url
            )
            embed_info = f"{openai_embed_model} via OpenAI ({args.embed_dim}d)"
        else:
            embed_fn = lambda text: _ollama_embed(text, embed_model, args.ollama_url)
            embed_info = f"{embed_model} via Ollama ({args.embed_dim}d)"

        print("\n[SQL-OUTPUT MODE] No Oracle connection — generating SQL script.")

        print("\n[1/4] Chunking documents …")
        all_chunks: list[str] = []
        source_names: list[str] = []
        chunk_entries: list[dict[str, object]] = []
        for doc_path in args.documents:
            if not doc_path.exists():
                print(f"  [error] File not found: {doc_path}", file=sys.stderr)
                sys.exit(1)
            print(f"  {doc_path.name}")
            chunks = chunk_document(doc_path)
            all_chunks.extend(chunks)
            source_names.append(doc_path.name)
            chunk_entries.extend(
                {
                    "text": chunk_text,
                    "metadata": {"ref": doc_path.name},
                }
                for chunk_text in chunks
            )
            print(f"    → {len(chunks)} chunks")
        write_chunks_json(source_names, chunk_entries)
        source_docs_str = ", ".join(source_names)

        print("\n[2/4] Extracting graph schema via LLM …")
        schema = extract_graph_schema(
            all_chunks,
            prompt_template,
            args.llm_provider,
            llm_model,
            args.ollama_url,
            openai_api_key=openai_api_key,
            openai_base_url=args.openai_base_url,
        )
        print(f"  Vertex types: {len(schema['vertex'])} | Edge types: {len(schema['edge'])} | Connections: {len(schema.get('connection', []))}")

        create_user_info: tuple[str, str] | None = None
        if args.create_user:
            print("\n[3/4] Collecting user creation info (no DB connection needed) …")
            username = input("  New username [graphuser]: ").strip() or "graphuser"
            password = getpass.getpass(f"  Password for {username} [Welcome12345]: ").strip() or "Welcome12345"
            create_user_info = (username, password)
            print(f"  User creation SQL will be included in {sql_path} (run as DBA first).")
        else:
            print("\n[3/4] Skipping user creation (no --create-user flag).")

        print(f"\n[4/4] Generating embeddings and building SQL script → {sql_path} …")
        sql_content = generate_sql_script(
            schema=schema,
            graph_name=args.graph_name,
            source_docs_str=source_docs_str,
            embed_fn=embed_fn,
            emb_dim=args.embed_dim,
            create_user_info=create_user_info,
            force_recreate=args.force_recreate,
        )
        sql_path.write_text(sql_content, encoding="utf-8")

        print("\n" + "=" * 60)
        print("SQL SCRIPT GENERATED")
        print("=" * 60)
        print(f"File          : {sql_path.resolve()}")
        print(f"Graph         : {args.graph_name}")
        print(f"Sources       : {source_docs_str}")
        print(f"Embeddings    : {embed_info}")
        if create_user_info:
            print(f"User creation : included — run that block as DBA/SYSDBA first")
        print()
        print("To execute via SQLcl MCP, tell Claude:")
        print(f'  "Use SQLcl connection <name> to run {sql_path.resolve()}"')
        print("=" * 60)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # DIRECT MODE: connect to Oracle and execute immediately
    # ══════════════════════════════════════════════════════════════════════════

    # ── Step 1: Connect to Oracle ─────────────────────────────────────────────
    print("\n[1/6] Connecting to Oracle DB …")
    conn = oracledb.connect(user=args.db_user, password=args.db_password, dsn=args.db_dsn)
    print(f"  Connected  (Oracle {conn.version})")

    # ── Optional: create a dedicated user ─────────────────────────────────────
    if args.create_user:
        new_user, new_pass = create_user_interactively(conn)
        conn.close()
        # Reconnect as the newly created user for all subsequent steps
        print(f"  Reconnecting as {new_user} …")
        conn = oracledb.connect(user=new_user, password=new_pass, dsn=args.db_dsn)
        print(f"  Connected as {new_user.upper()}")

    # ── Step 2: Chunk all documents ───────────────────────────────────────────
    print("\n[2/6] Chunking documents …")
    all_chunks: list[str] = []
    source_names: list[str] = []
    chunk_entries: list[dict[str, object]] = []
    for doc_path in args.documents:
        if not doc_path.exists():
            print(f"  [error] File not found: {doc_path}", file=sys.stderr)
            sys.exit(1)
        print(f"  {doc_path.name}")
        chunks = chunk_document(doc_path)
        all_chunks.extend(chunks)
        source_names.append(doc_path.name)
        chunk_entries.extend(
            {
                "text": chunk_text,
                "metadata": {"ref": doc_path.name},
            }
            for chunk_text in chunks
        )
        print(f"    → {len(chunks)} chunks")
    print(f"  Total: {len(all_chunks)} chunks from {len(source_names)} document(s)")
    write_chunks_json(source_names, chunk_entries)
    source_docs_str = ", ".join(source_names)

    # ── Step 3: Extract graph schema via LLM ──────────────────────────────────
    print("\n[3/6] Extracting graph schema via LLM …")
    schema = extract_graph_schema(
        all_chunks,
        prompt_template,
        args.llm_provider,
        llm_model,
        args.ollama_url,
        openai_api_key=openai_api_key,
        openai_base_url=args.openai_base_url,
    )
    n_entity_instances = sum(len(v.get("properties", [])) for v in schema["vertex"])
    print(f"  Vertex types      : {len(schema['vertex'])}")
    print(f"  Entity instances  : {n_entity_instances}")
    print(f"  Edge types        : {len(schema['edge'])}")
    print(f"  Connections       : {len(schema.get('connection', []))}")

    # ── Step 4: Prepare Oracle schema ─────────────────────────────────────────
    print("\n[4/6] Preparing Oracle schema …")
    already_exists = _graph_exists(conn, args.graph_name)
    if args.force_recreate and already_exists:
        print(f"  Dropping existing graph {args.graph_name} …")
        drop_schema(conn, schema, args.graph_name)
        already_exists = False

    if not already_exists:
        edge_connections = create_schema(conn, schema, args.graph_name, args.embed_dim)
    else:
        print(f"  Graph {args.graph_name} already exists — populating without DDL changes")
        edge_connections = _build_edge_connections(schema)

    # ── Step 5: Insert data + embeddings ──────────────────────────────────────
    print("\n[5/6] Inserting entities and relationships with embeddings …")
    if effective_embed_provider == "oracle":
        embed_fn = lambda text: _oracle_embed(conn, text, args.oracle_embed_model)
        embed_info = f"Oracle DBMS_VECTOR / {args.oracle_embed_model}"
    elif effective_embed_provider == "openai":
        embed_fn = lambda text: _openai_embed(
            text, openai_embed_model, openai_api_key, args.openai_base_url
        )
        embed_info = f"{openai_embed_model} via OpenAI ({args.embed_dim}d)"
    else:
        embed_fn = lambda text: _ollama_embed(text, embed_model, args.ollama_url)
        embed_info = f"{embed_model} via Ollama ({args.embed_dim}d)"

    print("  Vertices:")
    entity_ids = insert_entities(conn, schema, source_docs_str, embed_fn, args.embed_dim)

    print("  Edges:")
    insert_relationships(conn, schema, entity_ids, edge_connections, embed_fn, args.embed_dim)

    # ── Step 6: Summary ───────────────────────────────────────────────────────
    print("\n[6/6] Done.")
    print("\n" + "=" * 60)
    print("GRAPH BUILD COMPLETE")
    print("=" * 60)
    print(f"Graph         : {args.graph_name}")
    print(f"Documents     : {source_docs_str}")
    print(f"Chunks        : {len(all_chunks)}")
    print(f"Vertex types  : {', '.join(v['label'] for v in schema['vertex'])}")
    print(f"Entities      : {len(entity_ids)}")
    print(f"Edge types    : {', '.join(e['label'] for e in schema['edge'])}")
    print(f"Connections   : {len(schema.get('connection', []))}")
    print(f"Embeddings    : {embed_info}")
    print()

    # Print a starter PGQL query (Oracle 23ai-safe; avoid LABEL(e)).
    print("Sample PGQL query (Oracle 23ai):")
    print("  SELECT *")
    print("  FROM GRAPH_TABLE (")
    print(f"    {args.graph_name}")
    print("    MATCH (s)-[e]->(t)")
    print("    COLUMNS (")
    print("      VERTEX_ID(s)  AS src_id,")
    print("      s.name        AS src_name,")
    print("      EDGE_ID(e)    AS edge_id,")
    print("      e.description AS edge_desc,")
    print("      VERTEX_ID(t)  AS dst_id,")
    print("      t.name        AS dst_name")
    print("    )")
    print("  )")
    print("  FETCH FIRST 25 ROWS ONLY;")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
