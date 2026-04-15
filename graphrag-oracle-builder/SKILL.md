---
name: graphrag-oracle-builder
description: >
  Build a GraphRAG knowledge base from documents into an Oracle Property Graph (23ai–26ai).
  Use this skill whenever the user wants to: ingest documents into a graph database, build
  a GraphRAG pipeline, extract entities and relationships from text, populate an Oracle
  Property Graph, add vector embeddings to graph vertices or edges, or set up graph-based
  RAG (Retrieval-Augmented Generation). Trigger even if the user says "create the graph from
  my docs", "build a Graph for RAG in Oracle", "generate setup SQL for graph", or "extract entities and relationship from a document and buil a property graph in Oracle DB".
---

# GraphRAG Oracle Builder

This skill runs a complete GraphRAG ingestion pipeline:

1. **Chunk** one or more documents (any format Docling supports: PDF, DOCX, HTML, XHTML,XLSX, PPTX, Markdown, PNG, JPEG, TIFF, BMP, WEBP, …)
2. **Extract** entities and relationships from chunks via an LLM
3. **Create or reuse** an Oracle Property Graph schema (tables + `CREATE PROPERTY GRAPH`)
4. **Insert** entity vertices and relationship edges
5. **Embed** every vertex and every edge using Ollama embeddings model, OpenAI embeddings model, or Oracle `DBMS_VECTOR` embedding model
6. **Create or reuse** a Langchain vector store for Oracle DB from chunk created at step 1. 
7. **Print** a summary of the graph built

---

## Mandatory Rules

- NEVER apply a generated `*_setup.sql` to a database without explicit user confirmation in the current session.
- For DB command execution, TRY SQLcl MCP first (`mcp__sqlcl__connect` + `mcp__sqlcl__run_sqlcl` / `mcp__sqlcl__run_sql`).
- If SQLcl MCP is unavailable or fails, fall back to direct `oracledb`-based execution paths.
- Do not assume default Ollama models. Always discover available local models first via `scripts/list_ollama_models.py` and ask the user to choose.
- Do not assume default OpenAI embedding models. When OpenAI embeddings are selected, always require an explicit OpenAI embedding model name from the user.
- In generated setup SQL scripts, always use `<GRAPH-NAME>_V_` as vertex table prefix and `<GRAPH-NAME>_E_` as edge table prefix, where <GRAPH-NAME> is provided by user or set to MY_KG by default.
- Always ask for graph name first, then check in Oracle DB whether that graph already exists before extraction starts.
- Always perform graph existence checks using this case-insensitive query pattern:
  - `SELECT graph_name FROM user_property_graphs WHERE UPPER(graph_name) = UPPER('<GRAPH_NAME>')`
- If graph exists: constrain extraction to existing vertex/edge types and generate append-only SQL (`--append-existing`) so only new rows are inserted.
- If graph does not exist: keep the current `graph_extract.py` behavior unchanged.
- Extraction scope prompt must be conditional:
  - If graph exists: do NOT ask extraction scope (`autonomous` or `vertex_types=<N>,edge_types=<M>`).
  - If graph does not exist: ask extraction scope details before extraction.
- Always ask the user which input file(s) must be chunked and used to create the graph before starting chunking.
- Use Codex as the default extraction provider (`--llm-provider codex`).
- All generated artifacts must be created under `./temp` (for example: `temp/output_chunks.json`, `temp/codex_prompt_chunks.txt`, `temp/output_schema.json`, `temp/*_setup.sql`).
- For extraction, always build a merged prompt from `scripts/prompt_text_to_graph.txt` + `temp/output_chunks.json` into `temp/codex_prompt_chunks.txt`.
- In Codex mode, skip in-script extraction and prompt `temp/codex_prompt_chunks.txt` with Codex to produce the extraction JSON file (`temp/output_schema.json`, extract_graph_schema-compatible).
- `temp/output_schema.json` MUST include chunk UUID provenance for extraction evidence:
  - `vertex[].properties[].uuids` (required)
  - `vertex[].uuids` (recommended aggregate)
  - `edge[].uuids` (required)
  - `connection` may be either `["src","EDGE","dst"]` or `{"triple":[...], "uuids":[...]}`.
- If execution mode is `generate_and_execute_sql`, confirm the target SQLcl connection name before execution and run a post-apply verification query.
- LangChain vector store ingestion (Phase 4) is mandatory in the pipeline:
  - In `generate_and_execute_sql`, always execute Phase 4 after graph SQL apply.
  - In `generate_sql_only`, always produce the exact Phase 4 command block (ready to run) and explicitly mark it as pending execution.
- After successful DB graph creation, ALWAYS provide a final example PGQL query using the exact template in `Mandatory final step after DB graph creation`.

---

## Mandatory Preflight (Required Inputs)

Before running any command, ask these mandatory questions and wait for valid answers.
Do not start chunking, extraction, or store until all answers are collected.

Use this exact concise prompt format (no numbered menus):

- Graph name (free text, non-empty)
- LLM provider: `codex`, `openai`, `ollama`
- Execution mode: `generate_sql_only`, `generate_and_execute_sql`
- Oracle username (free text, non-empty)
- Does that Oracle username already exist? `yes`, `no`
- Input file(s) to chunk and ingest (required).

Include this reply instruction exactly:
`Reply in one line like: MY_KG,codex,generate_and_execute_sql,graphuser,yes,graphrag_example.pdf`

Validation rules:
- Accept only one value per required field.
- If an answer is missing or invalid, ask again only for that field.
- Never assume defaults for required fields.

Execution gate:
- Continue only after all 6 inputs are valid.
- Echo the selected values in one summary line before starting.
- If mode is `generate_and_execute_sql`, ask for SQLcl connection name before execution, then ask for explicit confirmation immediately before running `mcp__sqlcl__run_sqlcl`.

If `request_user_input` tool is available (Plan mode), use equivalent strict options but keep labels and values aligned with this same non-numbered format.

---

## Prerequisites

### 0 — Cleanup previous session

Run this always inside the `graphrag-oracle-builder/` directory:
```bash
rm -f temp/output_chunks.json temp/output_schema.json temp/codex_prompt_chunks.txt temp/*.sql
```

### 1 — Python virtual environment

Run this **once** inside the `graphrag-oracle-builder/` directory:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m ensurepip --upgrade
python -m pip install -U pip
python -m pip install docling oracledb requests langchain-community
```

All subsequent script invocations must use `.venv/bin/python` (not the system `python3`):

```bash
.venv/bin/python scripts/graph_user.py ...
.venv/bin/python scripts/graph_extract.py ...
.venv/bin/python scripts/graph_store.py ...
```

### 2 — LLM provider setup (Codex default, OpenAI/Ollama optional)

By default the script uses Codex prompt handoff for graph extraction (`--llm-provider codex`).
In this mode, `graph_extract.py` prepares `temp/codex_prompt_chunks.txt` and does not call an API.

If you choose OpenAI (`--llm-provider openai`), set `OPENAI_API_KEY` before running:

```bash
export OPENAI_API_KEY=...
```

If you choose OpenAI embeddings in store/full pipeline mode (`--embed-provider openai`),
`OPENAI_API_KEY` is also required and you must explicitly provide/select
`--openai-embed-model` (no implicit default is allowed).

If you choose Ollama (`--llm-provider ollama`), make sure Ollama is running and models
are pulled:

Before selecting any Ollama model, list available models with the fixed helper script:

```bash
.venv/bin/python scripts/list_ollama_models.py
```

Then ask the user to choose models explicitly. No implicit default Ollama model is allowed.
When Ollama is selected, the extraction script prompts for one graph-generation model, and store phase prompts for one embedding model.
When OpenAI embeddings are selected, the store/full pipeline must prompt for an explicit OpenAI embedding model name.

Ollama must be running locally (or at a custom URL): if not find ollama running, ask for the custom URL.


### 3 — Oracle DB

Oracle 23ai or later required (VECTOR data type + Property Graph DDL).

When Codex runs the skill it should:
1. Check whether `.venv/bin/python` exists in the skill directory.
2. If not, run the venv setup commands above before proceeding.
3. Run the split pipeline scripts in order: `graph_user.py` (optional), `graph_extract.py`, `graph_store.py`, `chunks_to_langchain_oracle_vs.py` (mandatory).

---

## Running the pipeline (split applications)

### Phase 0 — user check/create (optional)

Use this phase only for Oracle user validation or interactive creation, then stop
before chunking:

```bash
.venv/bin/python scripts/graph_user.py \
  --db-user sys --db-password syspass --db-dsn host:1521/FREEPDB1 \
  --create-user
```

### Phase 1 — extract artifacts

Run chunking + extraction. This phase always creates:
- `temp/output_chunks.json`
- `temp/codex_prompt_chunks.txt` (prompt template merged with chunk content)
- `temp/output_schema.json` (same JSON structure as `extract_graph_schema()`)

```bash
.venv/bin/python scripts/graph_extract.py \
  --graph-name <KNOWLEDGE_GRAPH> \
  --db-user <ORACLE_USER> \
  --db-password <ORACLE_PASSWORD> \
  --db-dsn <HOST:PORT/SERVICE> \
  --llm-provider codex \
  --chunks-output temp/output_chunks.json \
  --merged-prompt-output temp/codex_prompt_chunks.txt \
  --schema-output temp/output_schema.json \
  doc1.pdf doc2.docx
```

Graph existence check behavior for this phase:
- When `--graph-name` + DB credentials are provided, `graph_extract.py` checks `USER_PROPERTY_GRAPHS` before extraction.
- The check is case-insensitive (`UPPER(graph_name) = UPPER(:graph_name)`).

- Mandatory conditional extraction-scope question:
  - Ask `autonomous` or `vertex_types=<N>,edge_types=<M>` only when the graph does NOT already exist.
  - When the graph already exists, skip this question and constrain extraction to existing graph types.

In Codex mode the script skips in-script extraction. Prompt Codex with
`temp/codex_prompt_chunks.txt` and save the JSON response to `temp/output_schema.json`.
Before continuing, verify entity provenance is present in `temp/output_schema.json`
(`vertex[].properties[].uuids` with chunk UUIDs from `temp/output_chunks.json`).

If extraction must be done manually with another provider, generate only artifacts:

```bash
.venv/bin/python scripts/graph_extract.py \
  --graph-name <KNOWLEDGE_GRAPH> \
  --db-user <ORACLE_USER> \
  --db-password <ORACLE_PASSWORD> \
  --db-dsn <HOST:PORT/SERVICE> \
  --skip-llm \
  --chunks-output temp/output_chunks.json \
  --merged-prompt-output temp/codex_prompt_chunks.txt \
  --schema-output temp/output_schema.json \
  doc1.pdf
```

In manual mode, the LLM response must be saved as `temp/output_schema.json`.

### Phase 2 — generate SQL setup script

- Mandatory: Ask for the name of Graph: <GRAPH_NAME>
- Mandatory: Ask for embedding provider (`ollama` or `openai`).
- Mandatory: If embedding provider is `openai`, ask for explicit OpenAI embedding model name (no default).

- `graph_store.py` reads the extraction schema from file and generates SQL. Replace `<KNOWLEDGE_GRAPH>` with the actual graph name.
- Choose embedding provider explicitly:

Ollama embeddings:

```bash
.venv/bin/python scripts/graph_store.py \
  --schema-input temp/output_schema.json \
  --chunks-input temp/output_chunks.json \
  --graph-name <KNOWLEDGE_GRAPH> \
  --db-user <ORACLE_USER> \
  --db-password <ORACLE_PASSWORD> \
  --db-dsn <HOST:PORT/SERVICE> \
  --sql-output temp/graphrag_setup.sql \
  --embed-provider ollama \
  --embed-model nomic-embed-text \
  --embed-dim 768
```

OpenAI embeddings:

```bash
export OPENAI_API_KEY=...
.venv/bin/python scripts/graph_store.py \
  --schema-input temp/output_schema.json \
  --chunks-input temp/output_chunks.json \
  --graph-name <KNOWLEDGE_GRAPH> \
  --db-user <ORACLE_USER> \
  --db-password <ORACLE_PASSWORD> \
  --db-dsn <HOST:PORT/SERVICE> \
  --sql-output temp/graphrag_setup.sql \
  --embed-provider openai \
  --openai-embed-model text-embedding-3-large \
  --embed-dim 3072
```

Optional flags:
- `--create-user` to include a user-creation SQL block
- `--force-recreate` to include drop/recreate SQL
- `--append-existing` to generate INSERT-only SQL for an already existing graph (no DDL)

Graph existence check behavior for this phase:
- If `--db-user/--db-password/--db-dsn` are provided, `graph_store.py` always checks `USER_PROPERTY_GRAPHS`.
- If graph exists, `graph_store.py` automatically forces `--append-existing` mode.
- In `--append-existing` mode, SQL generation aligns edge insert direction with existing edge table FK direction (`src_v_id`/`dst_v_id`) to avoid ORA-02291 when extraction triples are reversed.
- If graph does not exist and `--append-existing` was set, `graph_store.py` fails fast.

### Phase 3 - SQLcl execution

Do not run generated SQL without explicit user confirmation. After approval:

```text
mcp__sqlcl__connect  connection_name=<graphrag-conn>
mcp__sqlcl__run_sqlcl  sqlcl=@temp/graphrag_setup.sql
```

Verify with:

```text
mcp__sqlcl__run_sql  sql=SELECT * FROM GRAPH_TABLE (<KNOWLEDGE_GRAPH> MATCH (n) COLUMNS (n.name)) FETCH FIRST 10 ROWS ONLY;
```

Recommended post-apply checks:
- List created graph tables: `SELECT table_name FROM user_tables WHERE table_name LIKE 'V_%' OR table_name LIKE 'E_%' ORDER BY table_name;`
- Count rows per table to catch missing edge inserts.

### Phase 4 - mandatory: store the chunks in oracle db as Langchain vector store

Store `temp/output_chunks.json` in an Oracle-backed LangChain vector store table.
Use the same embedding model selection used by `graph_store.py` (provider-consistent).
- Mandatory: Ask for vector store table name first: `<LANGCHAIN_CHUNKS_VS>` to pass as `--table-name`.
- If the table already exists in Oracle, append new chunks to it.
- Execution-mode behavior:
  - In `generate_and_execute_sql`: run this phase immediately after Phase 3.
  - In `generate_sql_only`: print this exact command with resolved values and label it `PENDING EXECUTION`.

```bash
.venv/bin/python scripts/chunks_to_langchain_oracle_vs.py \
  --chunks-input temp/output_chunks.json \
  --db-user graphuser \
  --db-password mypass \
  --db-dsn host:1521/FREEPDB1 \
  --table-name <LANGCHAIN_CHUNKS_VS> \
  --embed-model nomic-embed-text:latest
```

Notes:
- `--embed-model` is required by policy (explicit model selection, no implicit default).
- `--table-name` must be asked explicitly (no implicit table choice).
- Keep the same embedding model used in Phase 2 (`graph_store.py`) for retrieval consistency.
- The script stores chunk text plus metadata (`uuid`, `ref`) as LangChain documents.

### Phase 5 - show a SQL query on graph DB created

After the graph is created in Oracle DB, always propose this example query AS-IS, replacing `<KNOWLEDGE_GRAPH>` with the actual graph name:

Oracle DB 23ai compatibility rule:
- Do **not** use `LABEL(e)` in `GRAPH_TABLE` examples.
- Use `EDGE_ID(e)` plus edge properties (for example `e.description`) instead.

```sql
SELECT *
FROM GRAPH_TABLE (
  <KNOWLEDGE_GRAPH>
  MATCH (s)-[e]->(t)
  COLUMNS (
    VERTEX_ID(s)  AS src_id,
    s.name        AS src_name,
    EDGE_ID(e)    AS edge_id,
    e.description AS edge_desc,
    VERTEX_ID(t)  AS dst_id,
    t.name        AS dst_name
  )
);
```

### Key options

| Script | Option | Default | Description |
|---|---|---|---|
| `graph_user.py` | `--db-user/--db-password/--db-dsn` | required | Oracle connection for user check/create |
| `graph_user.py` | `--create-user` | off | Interactive Oracle user creation |
| `graph_extract.py` | `--llm-provider` | `codex` | Extraction provider: `codex`, `openai`, or `ollama` |
| `graph_extract.py` | `--llm-model` | prompt-required | Extraction model for Ollama |
| `graph_extract.py` | `--openai-model` | script default | Extraction model for OpenAI |
| `graph_extract.py` | `--prompt` | bundled | Path to extraction prompt template |
| `graph_extract.py` | `--chunks-output` | `temp/output_chunks.json` | Chunk output JSON path |
| `graph_extract.py` | `--merged-prompt-output` | `temp/codex_prompt_chunks.txt` | Merged prompt output path |
| `graph_extract.py` | `--schema-output` | `temp/output_schema.json` | Extracted schema JSON output path |
| `graph_extract.py` | `--skip-llm` | off | Only write chunk + merged prompt artifacts |
| `graph_extract.py` | `--graph-name` | off | Graph name to check in Oracle before extraction |
| `graph_extract.py` | `--db-user/--db-password/--db-dsn` | off | Oracle connection used for graph existence/type checks |
| `graph_store.py` | `--schema-input` | `temp/output_schema.json` | Extraction schema input file |
| `graph_store.py` | `--chunks-input` | `temp/output_chunks.json` | Chunk metadata input file |
| `graph_store.py` | `--graph-name` | required | Oracle Property Graph name |
| `graph_store.py` | `--sql-output` | `temp/graphrag_setup.sql` | Generated SQL file path |
| `graph_store.py` | `--embed-provider` | `ollama` | Embedding provider: `ollama` or `openai` |
| `graph_store.py` | `--embed-model` | prompt-required | Ollama embedding model (`--embed-provider ollama`) |
| `graph_store.py` | `--openai-embed-model` | prompt-required | OpenAI embedding model (`--embed-provider openai`) |
| `graph_store.py` | `--openai-base-url` | `https://api.openai.com/v1` | OpenAI base URL for embeddings |
| `graph_store.py` | `--embed-dim` | `768` | Embedding dimension |
| `graph_store.py` | `--create-user` | off | Include user creation block in SQL |
| `graph_store.py` | `--force-recreate` | off | Include drop/recreate SQL |
| `graph_store.py` | `--append-existing` | off | Insert-only SQL for existing graph (no CREATE statements) |
| `chunks_to_langchain_oracle_vs.py` | `--chunks-input` | `temp/output_chunks.json` | Chunk input JSON path |
| `chunks_to_langchain_oracle_vs.py` | `--db-user/--db-password/--db-dsn` | required | Oracle connection for vector store writes |
| `chunks_to_langchain_oracle_vs.py` | `--table-name` | `LANGCHAIN_CHUNKS_VS` | Oracle LangChain vector store table (explicitly ask, no implicit choice) |
| `chunks_to_langchain_oracle_vs.py` | `--embed-model` | prompt-required | Ollama embedding model |
| `chunks_to_langchain_oracle_vs.py` | `--ollama-url` | `http://localhost:11434` | Ollama base URL |

### Schema behaviour

- `graph_store.py` generates full DDL + DML by default; with `--append-existing` it emits INSERT-only SQL.
- Use `--force-recreate` to include a drop block before DDL.
- Existing-data handling is controlled by how and where you execute the generated SQL.

### Embedding models and dimensions

| Model               | `--embed-dim`      |
|---------------------|--------------------|
| `nomic-embed-text`  |                 768|
| `mxbai-embed-large` |                1024|
| `all-minilm`        |                 384|
| Oracle ONNX (varies)| set accordingly    |

---

## What the pipeline produces

**In Oracle DB:**
- Generated setup SQL uses one vertex table per entity type: `V_<label>` (columns: `v_id`, `name`, `source_doc`, `embedding`)
- Generated setup SQL uses one edge table per relationship type: `E_<label>` (columns: `edge_id`, `src_v_id`, `dst_v_id`, `description`, `embedding`)
- A `CREATE PROPERTY GRAPH` definition linking tables via PGQL labels

**On stdout:**
- Per-step progress
- Final summary: include a detailed list of created vertex types and edge types (each with a brief description), plus total entity count, total connection count, and embedding model/dimension info.
- A sample PGQL query to get started

---

## Extending the prompt

The entity extraction prompt lives in `scripts/prompt_text_to_graph.txt`. Edit it to:
- Restrict or expand which entity types to extract
- Add domain-specific extraction rules
- Change the output schema (keep the JSON structure)

The `{{INSERT_TEXT_CHUNKS_HERE}}` placeholder is replaced at runtime with the full chunk text.
