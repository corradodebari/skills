#!/usr/bin/env python3
"""
graph_extract.py — GraphRAG extraction phase

This script runs chunking and extraction only:
  1. Chunk docs and persist temp/output_chunks.json
  2. Merge prompt_text_to_graph.txt + temp/output_chunks.json into a concrete prompt file
  3. Either:
     - codex mode (default): stop after writing temp/codex_prompt_chunks.txt
     - openai/ollama mode: call provider and write extract_graph_schema-compatible JSON to temp/output_schema.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import oracledb

from file_to_chunks import chunk_document
from graphrag_builder import (
    _graph_exists,
    get_existing_graph_types,
    enrich_schema_with_chunk_uuids,
    _ollama_generate,
    _ollama_list_models,
    _openai_generate,
    _prompt_ollama_model_required,
    write_chunks_json,
)


def _parse_schema_json(raw: str) -> dict:
    """Parse LLM JSON output, stripping markdown fences if present."""
    text = raw.strip()
    if not text:
        raise RuntimeError("LLM returned an empty response while JSON schema was expected.")
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback for chatty models that wrap JSON with natural-language text.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _build_existing_types_constraints(vertex_labels: list[str], edge_labels: list[str]) -> str:
    vertex_list = ", ".join(vertex_labels) if vertex_labels else "(none)"
    edge_list = ", ".join(edge_labels) if edge_labels else "(none)"
    return (
        "EXISTING GRAPH TYPE CONSTRAINTS\n"
        "The target Oracle Property Graph already exists.\n"
        "Extract only entities and relations that map to existing graph types.\n"
        f"- Allowed vertex labels: {vertex_list}\n"
        f"- Allowed edge labels: {edge_list}\n"
        "Do not propose new vertex labels or new edge labels.\n"
        "Discard evidence that cannot be represented with the allowed labels.\n"
    )


def _build_merged_prompt(
    prompt_template: str,
    chunks_payload: dict,
    existing_constraints: str | None = None,
) -> str:
    chunks = chunks_payload.get("chunks", [])
    if not isinstance(chunks, list):
        raise RuntimeError("Invalid output_chunks.json: 'chunks' must be a list.")
    payload_text = json.dumps(chunks_payload, ensure_ascii=False, indent=2)
    template = prompt_template
    if existing_constraints:
        template = existing_constraints + "\n\n" + template
    return template.replace("{{INSERT_TEXT_CHUNKS_HERE}}", payload_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GraphRAG extract phase: chunk docs, build merged prompt, generate schema JSON"
    )
    parser.add_argument("documents", nargs="+", type=Path, help="Input documents (Docling-supported formats)")
    parser.add_argument(
        "--llm-provider",
        choices=["codex", "ollama", "openai"],
        default="codex",
        help="LLM provider for entity/relation extraction (codex is default prompt-handoff mode)",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Ollama LLM model for extraction (optional; otherwise selected interactively)",
    )
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
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    parser.add_argument("--prompt", type=Path, default=None, help="Prompt template path")
    parser.add_argument(
        "--chunks-output",
        type=Path,
        default=Path("temp/output_chunks.json"),
        help="Chunk JSON output path",
    )
    parser.add_argument(
        "--merged-prompt-output",
        type=Path,
        default=Path("temp/codex_prompt_chunks.txt"),
        help="Merged prompt output path",
    )
    parser.add_argument(
        "--schema-output",
        type=Path,
        default=Path("temp/output_schema.json"),
        help="Output file for extracted graph schema JSON",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Only generate output_chunks.json and merged prompt file; skip schema extraction",
    )
    parser.add_argument("--graph-name", default=None, help="Oracle Property Graph name to check before extraction")
    parser.add_argument("--db-user", default=None, help="Oracle username for graph existence/type checks")
    parser.add_argument("--db-password", default=None, help="Oracle password for graph existence/type checks")
    parser.add_argument("--db-dsn", default=None, help="Oracle DSN for graph existence/type checks")
    args = parser.parse_args()

    db_check_values = [args.db_user, args.db_password, args.db_dsn]
    provided_db_fields = sum(1 for value in db_check_values if value)
    if provided_db_fields not in (0, 3):
        raise RuntimeError(
            "For Oracle graph checks, provide all of --db-user, --db-password, and --db-dsn."
        )
    if args.graph_name and provided_db_fields != 3:
        raise RuntimeError(
            "--graph-name requires --db-user, --db-password, and --db-dsn for graph existence checks."
        )

    llm_model = args.openai_model if args.llm_provider == "openai" else args.llm_model
    openai_api_key = os.getenv("OPENAI_API_KEY") if args.llm_provider == "openai" else None

    if args.llm_provider == "ollama":
        try:
            available_models = _ollama_list_models(args.ollama_url)
        except Exception as exc:
            raise RuntimeError(
                f"Could not fetch Ollama model list from {args.ollama_url}: {exc}"
            ) from exc
        if not available_models:
            raise RuntimeError("No Ollama models discovered. Run scripts/list_ollama_models.py.")

        print("\nOllama models available:")
        for idx, model_name in enumerate(available_models, start=1):
            print(f"  {idx}. {model_name}")

        llm_model = _prompt_ollama_model_required(
            "Graph generation LLM", available_models, args.llm_model
        )

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

    args.chunks_output.parent.mkdir(parents=True, exist_ok=True)
    write_chunks_json(source_names, chunk_entries, args.chunks_output)

    existing_constraints: str | None = None
    if args.graph_name and args.db_user and args.db_password and args.db_dsn:
        print("\n[2/5] Checking target graph in Oracle DB …")
        conn = oracledb.connect(user=args.db_user, password=args.db_password, dsn=args.db_dsn)
        try:
            if _graph_exists(conn, args.graph_name):
                vertex_labels, edge_labels = get_existing_graph_types(conn, args.graph_name)
                if not vertex_labels and not edge_labels:
                    raise RuntimeError(
                        f"Graph {args.graph_name} exists but no graph tables were discovered."
                    )
                existing_constraints = _build_existing_types_constraints(vertex_labels, edge_labels)
                print(
                    f"  Graph {args.graph_name} exists. "
                    f"Using {len(vertex_labels)} vertex type(s) and {len(edge_labels)} edge type(s)."
                )
            else:
                print(f"  Graph {args.graph_name} does not exist. Using default extraction flow.")
        finally:
            conn.close()

    print("\n[3/5] Building merged extraction prompt …")
    prompt_path = args.prompt or (Path(__file__).parent / "prompt_text_to_graph.txt")
    prompt_template = prompt_path.read_text(encoding="utf-8")
    chunks_payload = json.loads(args.chunks_output.read_text(encoding="utf-8"))
    merged_prompt = _build_merged_prompt(
        prompt_template,
        chunks_payload,
        existing_constraints=existing_constraints,
    )
    args.merged_prompt_output.parent.mkdir(parents=True, exist_ok=True)
    args.merged_prompt_output.write_text(merged_prompt, encoding="utf-8")
    print(f"  Merged prompt saved: {args.merged_prompt_output.resolve()}")

    if args.skip_llm or args.llm_provider == "codex":
        if args.llm_provider == "codex":
            print("\n[4/5] Codex provider selected — skipping in-script LLM extraction.")
        else:
            print("\n[4/5] LLM extraction skipped (--skip-llm).")
        print(
            f"[5/5] Prompt {args.merged_prompt_output.resolve()} with Codex and "
            f"save the JSON schema to {args.schema_output.resolve()}"
        )
        return

    print("\n[4/5] Extracting graph schema via LLM …")
    if args.llm_provider == "ollama":
        raw = _ollama_generate(merged_prompt, llm_model, args.ollama_url).strip()
    elif args.llm_provider == "openai":
        if not openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when --llm-provider openai is used."
            )
        raw = _openai_generate(
            merged_prompt,
            llm_model,
            openai_api_key,
            args.openai_base_url,
        ).strip()
    else:
        raise RuntimeError(f"Unsupported llm provider: {args.llm_provider}")

    schema = _parse_schema_json(raw)
    schema = enrich_schema_with_chunk_uuids(schema, chunks_payload)
    args.schema_output.parent.mkdir(parents=True, exist_ok=True)
    args.schema_output.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n[5/5] Extraction output written.")
    print(f"  Schema JSON: {args.schema_output.resolve()}")
    print(f"  Vertex types: {len(schema.get('vertex', []))}")
    print(f"  Edge types: {len(schema.get('edge', []))}")
    print(f"  Connections: {len(schema.get('connection', []))}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
