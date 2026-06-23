"""Configurable HTTP AI selector for validated AIP dictionary candidates."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .codec import Candidate

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
MAX_RESPONSE_SIZE = 4 * 1024 * 1024


class ExternalAIError(RuntimeError):
    pass


def candidate_prompt(candidates: Sequence[Candidate], limit: int = 128) -> str:
    dataset = [
        {
            "id": item.id,
            "length": len(item.data),
            "occurrences": item.occurrences,
            "estimated_saving": item.estimated_saving,
            "bytes_base64": base64.b64encode(item.data).decode("ascii"),
        }
        for item in candidates[:256]
    ]
    return (
        "Select repeated binary patterns for a lossless AIP compression dictionary. "
        "Maximize total byte savings and avoid redundant, overlapping, or contained "
        "patterns. Never invent bytes. Return JSON only as "
        f'{{"selected_ids":[1,2,3]}} with at most {limit} IDs.\n'
        "Candidate dataset:\n"
        + json.dumps(dataset, separators=(",", ":"))
    )


def _replace_values(value: Any, prompt: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{data}}", prompt)
    if isinstance(value, list):
        return [_replace_values(item, prompt) for item in value]
    if isinstance(value, dict):
        return {key: _replace_values(item, prompt) for key, item in value.items()}
    return value


def _json_values_from_text(text: str) -> list[Any]:
    """Parse plain JSON, JSON strings, fenced JSON, or JSON inside prose."""
    text = text.strip().lstrip("\ufeff")
    if not text:
        return []
    values: list[Any] = []
    try:
        values.append(json.loads(text))
    except json.JSONDecodeError:
        pass

    for match in re.finditer(
        r"```(?:json|javascript|js)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE
    ):
        fenced = match.group(1).strip()
        try:
            values.append(json.loads(fenced))
        except json.JSONDecodeError:
            continue

    # Some models wrap the answer in a sentence without a Markdown fence.
    decoder = json.JSONDecoder()
    for position, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            continue
        values.append(value)
        break
    return values


def _extract_selected_ids(value: Any, depth: int = 0) -> list[int] | None:
    if depth > 12:
        return None
    if isinstance(value, dict):
        ids = value.get("selected_ids")
        if isinstance(ids, list):
            return ids
        # Supports common chat API envelopes such as choices[].message.content,
        # Ollama response, and other nested JSON response shapes.
        for child in value.values():
            found = _extract_selected_ids(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_selected_ids(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, str):
        for parsed in _json_values_from_text(value):
            if parsed == value:
                continue
            found = _extract_selected_ids(parsed, depth + 1)
            if found is not None:
                return found
    return None


def select_candidates_via_api(
    candidates: Sequence[Candidate],
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body_template: str = "{{data}}",
    timeout: float = 90.0,
    limit: int = 128,
) -> list[int]:
    method = method.upper()
    if method not in ALLOWED_METHODS:
        raise ExternalAIError(f"unsupported HTTP method: {method}")
    if not url.startswith(("http://", "https://")):
        raise ExternalAIError("AI API URL must use http:// or https://")
    prompt = candidate_prompt(candidates, limit)
    if "{{data}}" not in url and "{{data}}" not in body_template and not any(
        "{{data}}" in value for value in (headers or {}).values()
    ):
        raise ExternalAIError("URL, header, or body must contain {{data}}")

    rendered_url = url.replace("{{data}}", quote(prompt, safe=""))
    rendered_headers = {
        key: str(_replace_values(value, prompt)) for key, value in (headers or {}).items()
    }
    data: bytes | None = None
    if method != "GET" or body_template:
        try:
            template_json = json.loads(body_template)
        except json.JSONDecodeError:
            rendered_body = body_template.replace("{{data}}", prompt)
        else:
            rendered_body = json.dumps(
                _replace_values(template_json, prompt), ensure_ascii=False, separators=(",", ":")
            )
            rendered_headers.setdefault("Content-Type", "application/json")
        data = rendered_body.encode("utf-8")

    request = Request(rendered_url, data=data, headers=rendered_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_SIZE + 1)
    except HTTPError as exc:
        detail = exc.read(1024).decode("utf-8", "replace")
        raise ExternalAIError(f"AI API returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ExternalAIError(f"AI API request failed: {exc}") from exc
    if len(raw) > MAX_RESPONSE_SIZE:
        raise ExternalAIError("AI API response is too large")
    text = raw.decode("utf-8", "replace")
    ids = _extract_selected_ids(text)
    if ids is None:
        raise ExternalAIError(
            "AI API response did not contain usable selected_ids JSON"
        )

    allowed = {item.id for item in candidates}
    selected: list[int] = []
    for value in ids:
        if type(value) is int and value in allowed and value not in selected:
            selected.append(value)
        if len(selected) >= limit:
            break
    if not selected and candidates:
        raise ExternalAIError("AI API selected no usable candidates")
    return selected
