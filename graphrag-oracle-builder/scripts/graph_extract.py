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

from file_to_chunks import chunk_document
from graphrag_builder import (
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


def _build_merged_prompt(prompt_template: str, chunks_payload: dict) -> str:
    chunks = chunks_payload.get("chunks", [])
    if not isinstance(chunks, list):
        raise RuntimeError("Invalid output_chunks.json: 'chunks' must be a list.")
    payload_text = json.dumps(chunks_payload, ensure_ascii=False, indent=2)
    return prompt_template.replace("{{INSERT_TEXT_CHUNKS_HERE}}", payload_text)


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
    args = parser.parse_args()

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

    print("\n[2/4] Building merged extraction prompt …")
    prompt_path = args.prompt or (Path(__file__).parent / "prompt_text_to_graph.txt")
    prompt_template = prompt_path.read_text(encoding="utf-8")
    chunks_payload = json.loads(args.chunks_output.read_text(encoding="utf-8"))
    merged_prompt = _build_merged_prompt(prompt_template, chunks_payload)
    args.merged_prompt_output.parent.mkdir(parents=True, exist_ok=True)
    args.merged_prompt_output.write_text(merged_prompt, encoding="utf-8")
    print(f"  Merged prompt saved: {args.merged_prompt_output.resolve()}")

    if args.skip_llm or args.llm_provider == "codex":
        if args.llm_provider == "codex":
            print("\n[3/4] Codex provider selected — skipping in-script LLM extraction.")
        else:
            print("\n[3/4] LLM extraction skipped (--skip-llm).")
        print(
            f"[4/4] Prompt {args.merged_prompt_output.resolve()} with Codex and "
            f"save the JSON schema to {args.schema_output.resolve()}"
        )
        return

    print("\n[3/4] Extracting graph schema via LLM …")
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

    print("\n[4/4] Extraction output written.")
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
