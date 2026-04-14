#!/usr/bin/env python3
"""
List local Ollama models in a deterministic, scriptable format.

Usage:
  python scripts/list_ollama_models.py
  python scripts/list_ollama_models.py --ollama-url http://localhost:11434
"""

from __future__ import annotations

import argparse
import json
import sys

import requests


def fetch_models(ollama_url: str) -> list[str]:
    resp = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return sorted(m["name"] for m in data.get("models", []) if m.get("name"))


def main() -> int:
    parser = argparse.ArgumentParser(description="List locally available Ollama models.")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON array instead of numbered text output",
    )
    args = parser.parse_args()

    try:
        models = fetch_models(args.ollama_url)
    except Exception as exc:
        print(f"[error] Failed to fetch Ollama models from {args.ollama_url}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(models))
        return 0

    if not models:
        print("No local Ollama models found.")
        return 0

    print("Ollama models:")
    for idx, model in enumerate(models, start=1):
        print(f"{idx}. {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
