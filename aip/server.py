"""Dependency-free local HTTP service for the AIP browser interface."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
import webbrowser

from .codec import AIPError, compress, decompress
from .bundle import unpack_files
from .external_ai import ExternalAIError, select_candidates_via_api
from .ollama import OllamaError, list_models, select_candidates

MAX_UPLOAD = 256 * 1024 * 1024
REQUEST_MAGIC = b"AIPR1"
MAX_CONFIG_SIZE = 1024 * 1024
WEB_FILE = Path(__file__).with_name("web") / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "AIP/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AIPError("invalid Content-Length") from exc
        if size < 0 or size > MAX_UPLOAD:
            raise AIPError(f"file exceeds the {MAX_UPLOAD // (1024 * 1024)} MiB server limit")
        body = self.rfile.read(size)
        if len(body) != size:
            raise AIPError("upload ended early")
        return body

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            body = WEB_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            self._json(200, {"ok": True, "version": "0.1.0", "maxUpload": MAX_UPLOAD})
        elif path == "/api/ollama/models":
            query = parse_qs(urlparse(self.path).query)
            try:
                models = list_models(url=query.get("url", ["http://127.0.0.1:11434"])[0])
                self._json(200, {"models": models})
            except OllamaError as exc:
                self._json(503, {"models": [], "error": str(exc)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._body()
            if parsed.path == "/api/compress":
                self._compress(body, parse_qs(parsed.query))
            elif parsed.path == "/api/decompress":
                self._decompress(body)
            else:
                self._json(404, {"error": "not found"})
        except (AIPError, OllamaError, ExternalAIError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # keep local UI responsive, without exposing a traceback
            self._json(500, {"error": f"internal error: {exc}"})

    @staticmethod
    def _compression_request(
        body: bytes, query: dict[str, list[str]]
    ) -> tuple[bytes, dict[str, object]]:
        if not body.startswith(REQUEST_MAGIC):
            return body, {
                "mode": "ollama" if query.get("ai", ["0"])[0] == "1" else "algorithm",
                "files": query.get("files", ["1"])[0],
                "model": query.get("model", ["qwen2.5:7b"])[0],
                "ollama_url": query.get("ollamaUrl", ["http://127.0.0.1:11434"])[0],
            }
        position = len(REQUEST_MAGIC)
        value = 0
        shift = 0
        for _ in range(10):
            if position >= len(body):
                raise AIPError("truncated compression request")
            current = body[position]
            position += 1
            value |= (current & 0x7F) << shift
            if not current & 0x80:
                break
            shift += 7
        else:
            raise AIPError("compression request config is too large")
        if value > MAX_CONFIG_SIZE or position + value > len(body):
            raise AIPError("invalid compression request config")
        try:
            config = json.loads(body[position : position + value])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AIPError("invalid compression request JSON") from exc
        if not isinstance(config, dict):
            raise AIPError("compression request config must be an object")
        return body[position + value :], config

    def _compress(self, body: bytes, query: dict[str, list[str]]) -> None:
        body, config = self._compression_request(body, query)
        mode = str(config.get("mode", "algorithm"))
        ai_message = "Algorithm-only candidate selection"
        try:
            if mode == "algorithm":
                selector = None
            elif mode == "ollama":
                model = str(config.get("model", ""))[:100]
                ollama_url = str(config.get("ollama_url", "http://127.0.0.1:11434"))
                selector = lambda candidates: select_candidates(
                    candidates, model=model, url=ollama_url
                )
                ai_message = f"Ollama model {model} selected candidates"
            elif mode == "api":
                api = config.get("api")
                if not isinstance(api, dict):
                    raise ExternalAIError("AI API configuration is missing")
                headers = api.get("headers", {})
                if not isinstance(headers, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in headers.items()
                ):
                    raise ExternalAIError("AI API headers must be a JSON string object")
                selector = lambda candidates: select_candidates_via_api(
                    candidates,
                    method=str(api.get("method", "POST")),
                    url=str(api.get("url", "")),
                    headers=headers,
                    body_template=str(api.get("body", "{{data}}")),
                )
                ai_message = "External AI API selected candidates"
            else:
                raise AIPError("unknown compression selection mode")
            result = compress(body, candidate_selector=selector)
        except (OllamaError, ExternalAIError) as exc:
            result = compress(body)
            ai_message = f"AI unavailable; algorithm fallback used ({exc})"
        self._binary(
            result.data,
            {
                "X-AIP-Original-Size": str(result.original_size),
                "X-AIP-Compressed-Size": str(result.compressed_size),
                "X-AIP-Dictionary-Entries": str(result.dictionary_entries),
                "X-AIP-Dictionary-Bytes": str(result.dictionary_bytes),
                "X-AIP-File-Count": str(config.get("files", "1")),
                "X-AIP-AI-Status": ai_message,
                "Access-Control-Expose-Headers": "X-AIP-Original-Size, X-AIP-Compressed-Size, X-AIP-Dictionary-Entries, X-AIP-Dictionary-Bytes, X-AIP-File-Count, X-AIP-AI-Status, X-AIP-Download-Name, X-AIP-Output-Type",
            },
        )

    def _decompress(self, body: bytes) -> None:
        restored = decompress(body)
        files = unpack_files(restored)
        if files is None:
            self._binary(
                restored,
                {
                    "X-AIP-Verified": "sha256",
                    "X-AIP-File-Count": "1",
                    "X-AIP-Download-Name": "restored.bin",
                },
            )
        elif len(files) == 1:
            self._binary(
                files[0].data,
                {
                    "X-AIP-Verified": "sha256",
                    "X-AIP-File-Count": "1",
                    "X-AIP-Download-Name": files[0].name,
                },
            )
        else:
            self._binary(
                restored,
                {
                    "X-AIP-Verified": "sha256",
                    "X-AIP-File-Count": str(len(files)),
                    "X-AIP-Download-Name": "AI-Package-restored.aipb",
                    "X-AIP-Output-Type": "aip-bundle",
                },
            )

    def _binary(self, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            # Response headers must be latin-1; status detail remains readable ASCII.
            self.send_header(key, value.encode("ascii", "replace").decode("ascii"))
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    address = f"http://{host}:{port}"
    print(f"AI Package UI: {address} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping AIP server")
    finally:
        server.server_close()
