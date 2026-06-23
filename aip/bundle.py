"""Multi-file payload format stored inside an AIP compressed stream."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from .codec import AIPError

BUNDLE_MAGIC = b"AIPB1"
MAX_FILES = 10_000
MAX_NAME_BYTES = 1024


@dataclass(frozen=True)
class BundledFile:
    name: str
    data: bytes


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _decode_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if position >= len(data):
            raise AIPError("truncated file bundle")
        current = data[position]
        position += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, position
        shift += 7
    raise AIPError("bundle varint is too large")


def _safe_name(name: str, index: int) -> str:
    # Browser uploads are flat. Strip path syntax again on the server so a
    # crafted bundle cannot write outside a destination directory.
    cleaned = PurePath(name.replace("\\", "/")).name.replace("\x00", "").strip()
    if cleaned in ("", ".", ".."):
        cleaned = f"file-{index + 1}.bin"
    return cleaned


def pack_files(files: list[BundledFile]) -> bytes:
    if not files or len(files) > MAX_FILES:
        raise AIPError("bundle must contain between 1 and 10,000 files")
    out = bytearray(BUNDLE_MAGIC)
    out.extend(_encode_varint(len(files)))
    for index, item in enumerate(files):
        name = _safe_name(item.name, index).encode("utf-8")
        if len(name) > MAX_NAME_BYTES:
            raise AIPError("file name is too long")
        out.extend(_encode_varint(len(name)))
        out.extend(name)
        out.extend(_encode_varint(len(item.data)))
        out.extend(item.data)
    return bytes(out)


def unpack_files(payload: bytes) -> list[BundledFile] | None:
    """Return None for legacy/single raw payloads."""
    if not payload.startswith(BUNDLE_MAGIC):
        return None
    position = len(BUNDLE_MAGIC)
    count, position = _decode_varint(payload, position)
    if not 1 <= count <= MAX_FILES:
        raise AIPError("invalid number of files in bundle")
    files: list[BundledFile] = []
    used_names: set[str] = set()
    for index in range(count):
        name_size, position = _decode_varint(payload, position)
        if not 1 <= name_size <= MAX_NAME_BYTES or position + name_size > len(payload):
            raise AIPError("invalid bundled file name")
        try:
            name = payload[position : position + name_size].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AIPError("bundled file name is not valid UTF-8") from exc
        position += name_size
        name = _safe_name(name, index)
        original = name
        suffix = 2
        while name.casefold() in used_names:
            stem, dot, extension = original.rpartition(".")
            name = f"{stem or original} ({suffix}){dot}{extension}" if dot else f"{original} ({suffix})"
            suffix += 1
        used_names.add(name.casefold())
        size, position = _decode_varint(payload, position)
        if position + size > len(payload):
            raise AIPError("truncated bundled file")
        files.append(BundledFile(name, payload[position : position + size]))
        position += size
    if position != len(payload):
        raise AIPError("unexpected bytes after file bundle")
    return files
