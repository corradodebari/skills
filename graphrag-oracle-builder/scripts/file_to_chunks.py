# USAGE:
# uv venv --python 3.11
# source .venv/bin/activate
# unalias python 2>/dev/null || true
# unalias pip 2>/dev/null || true
# hash -r
# python -m ensurepip --upgrade
# python -m pip install -U pip
# python -m pip install docling

# .venv/bin/python file_to_chunks.py [-o output.json] your-file.pdf
# ex:
# .venv/bin/python file_to_chunks.py -o graphrag_example.json graphrag_example.pdf


import argparse
import json
from pathlib import Path
from uuid import uuid4

from docling.chunking import HierarchicalChunker
from docling.datamodel.base_models import ConversionStatus
from docling.document_converter import DocumentConverter


def chunk_document(path: Path) -> list[str]:
    converter = DocumentConverter()
    result = converter.convert(source=str(path))

    if result.status not in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
        raise RuntimeError(
            f"Docling conversion failed for {path} with status {result.status.name}"
        )
    if result.document is None:
        raise RuntimeError(f"Docling did not return a document for {path}")

    chunker = HierarchicalChunker()
    chunks = [
        chunk.text.strip()
        for chunk in chunker.chunk(result.document)
        if chunk.text and chunk.text.strip()
    ]
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a document to JSON chunks using Docling."
    )
    parser.add_argument(
        "input_files",
        nargs="+",
        type=Path,
        help="Path(s) to the document(s) to convert",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Destination JSON file (defaults to temp/output_chunks.json)",
    )
    args = parser.parse_args()

    reference_docs: list[str] = []
    chunk_entries: list[dict[str, object]] = []
    total_chunks = 0
    for input_file in args.input_files:
        chunks = chunk_document(input_file)
        total_chunks += len(chunks)
        reference_docs.append(input_file.name)
        chunk_entries.extend(
            {
                "text": chunk_text,
                "metadata": {
                    "ref": input_file.name,
                    "uuid": str(uuid4()),
                },
            }
            for chunk_text in chunks
        )

    payload = {
        "reference_docs": list(dict.fromkeys(reference_docs)),
        "chunks": chunk_entries,
    }
    output_path = args.output or Path("temp/output_chunks.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Wrote {total_chunks} chunks from {len(payload['reference_docs'])} document(s) to {output_path}"
    )


if __name__ == "__main__":
    main()
