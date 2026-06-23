"""Command line interface for AI Package."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .codec import AIPError, compress, decompress
from .ollama import OllamaError, select_candidates


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aip", description="Compress and restore .aip files")
    commands = parser.add_subparsers(dest="command", required=True)

    pack = commands.add_parser("compress", aliases=["c"], help="create a .aip file")
    pack.add_argument("input", type=Path)
    pack.add_argument("output", type=Path, nargs="?")
    pack.add_argument("--ai", action="store_true", help="let local Ollama select dictionary patterns")
    pack.add_argument("--require-ai", action="store_true", help="fail instead of falling back if Ollama is unavailable")
    pack.add_argument("--model", default="qwen2.5:7b")
    pack.add_argument("--ollama-url", default="http://127.0.0.1:11434")

    unpack = commands.add_parser("decompress", aliases=["d"], help="restore a .aip file")
    unpack.add_argument("input", type=Path)
    unpack.add_argument("output", type=Path, nargs="?")

    serve = commands.add_parser("serve", help="start the browser UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", dest="open_browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        from .server import serve

        serve(args.host, args.port, open_browser=args.open_browser)
        return 0

    try:
        source = args.input.read_bytes()
        if args.command in ("compress", "c"):
            output = args.output or args.input.with_name(args.input.name + ".aip")
            selector = None
            if args.ai:
                selector = lambda candidates: select_candidates(
                    candidates, model=args.model, url=args.ollama_url
                )
            try:
                result = compress(source, candidate_selector=selector)
                ai_status = "Ollama" if selector else "deterministic"
            except OllamaError as exc:
                if args.require_ai:
                    raise
                print(f"warning: {exc}; using deterministic fallback", file=sys.stderr)
                result = compress(source)
                ai_status = "deterministic fallback"
            output.write_bytes(result.data)
            print(
                f"{args.input} -> {output}: {result.original_size:,} -> "
                f"{result.compressed_size:,} bytes ({result.ratio:.1%}), "
                f"dictionary={result.dictionary_entries}, mode={ai_status}"
            )
        else:
            default_name = args.input.name[:-4] if args.input.name.lower().endswith(".aip") else args.input.name + ".out"
            output = args.output or args.input.with_name(default_name)
            restored = decompress(source)
            output.write_bytes(restored)
            print(f"{args.input} -> {output}: {len(restored):,} bytes, checksum verified")
    except (OSError, AIPError, OllamaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
