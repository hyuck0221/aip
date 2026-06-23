"""Deterministic dictionary codec used by the AIP container format."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import zlib
from typing import Callable, Iterable, Sequence

MAGIC = b"AIP1"
VERSION = 1
FLAG_ZLIB = 1
MAX_DICTIONARY_ENTRIES = 256
MAX_ENTRY_SIZE = 4096
DEFAULT_MAX_OUTPUT = 1024 * 1024 * 1024
MAX_SCAN_WINDOWS = 65_536
LZ_MIN_MATCH = 6
LZ_MAX_MATCH = 1024 * 1024
LZ_MAX_DISTANCE = 16 * 1024 * 1024
LZ_CHAIN_LIMIT = 64
LZ_MAX_HASH_KEYS = 262_144


class AIPError(ValueError):
    """Raised when an AIP stream is invalid or unsafe to expand."""


@dataclass(frozen=True)
class Candidate:
    id: int
    data: bytes
    occurrences: int
    estimated_saving: int


@dataclass(frozen=True)
class CompressionResult:
    data: bytes
    original_size: int
    compressed_size: int
    dictionary_entries: int
    dictionary_bytes: int
    used_zlib: bool
    used_ai: bool = False
    ai_message: str = ""

    @property
    def ratio(self) -> float:
        return self.compressed_size / self.original_size if self.original_size else 0.0


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot be negative")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise AIPError("truncated AIP data")
        value = self.data[self.pos : self.pos + size]
        self.pos += size
        return value

    def byte(self) -> int:
        return self.take(1)[0]

    def varint(self) -> int:
        value = 0
        shift = 0
        for _ in range(10):
            current = self.byte()
            value |= (current & 0x7F) << shift
            if not current & 0x80:
                return value
            shift += 7
        raise AIPError("varint is too large")


def find_candidates(data: bytes, limit: int = 512) -> list[Candidate]:
    """Find profitable repeated byte sequences without trusting an LLM."""
    if len(data) < 8:
        return []

    candidates: dict[bytes, tuple[int, int]] = {}
    # Larger patterns are more valuable. A fractional stride controls memory while
    # four shifted passes still catch patterns that are not naturally aligned.
    for width in (512, 256, 128, 64, 32, 16, 8, 4):
        if width * 2 > len(data):
            continue
        # Bound the number of allocated byte slices for large inputs. The stride
        # still scans across the whole file, rather than only inspecting a prefix.
        stride = max(1, width // 4, (len(data) - width + 1) // MAX_SCAN_WINDOWS)
        seen: dict[bytes, int] = {}
        for pos in range(0, len(data) - width + 1, stride):
            chunk = data[pos : pos + width]
            seen[chunk] = seen.get(chunk, 0) + 1
        for chunk, count in seen.items():
            if count < 2:
                continue
            # Conservative estimate: reference opcode/index costs up to 3 bytes,
            # and the dictionary stores a length plus the bytes themselves.
            saving = count * (width - 3) - (width + 3)
            if saving > 0:
                previous = candidates.get(chunk)
                if previous is None or saving > previous[1]:
                    candidates[chunk] = (count, saving)

    ranked = sorted(candidates.items(), key=lambda item: (item[1][1], len(item[0])), reverse=True)
    return [
        Candidate(id=index, data=chunk, occurrences=stats[0], estimated_saving=stats[1])
        for index, (chunk, stats) in enumerate(ranked[:limit])
    ]


def _remove_redundant(entries: Iterable[bytes], limit: int) -> list[bytes]:
    chosen: list[bytes] = []
    for entry in sorted(set(entries), key=len, reverse=True):
        if not 4 <= len(entry) <= MAX_ENTRY_SIZE:
            continue
        # A shorter entry fully contained in an already selected entry usually
        # adds dictionary overhead without improving greedy matches.
        if any(entry in larger for larger in chosen):
            continue
        chosen.append(entry)
        if len(chosen) >= min(limit, MAX_DICTIONARY_ENTRIES):
            break
    return chosen


def _encode_tokens(data: bytes, dictionary: Sequence[bytes]) -> bytes:
    by_first: dict[int, list[tuple[int, bytes]]] = {}
    for index, entry in enumerate(dictionary):
        by_first.setdefault(entry[0], []).append((index, entry))
    for matches in by_first.values():
        matches.sort(key=lambda pair: len(pair[1]), reverse=True)

    out = bytearray()
    literal = bytearray()
    chains: dict[bytes, list[int]] = {}

    def flush_literal() -> None:
        if literal:
            out.append(0)
            out.extend(_encode_varint(len(literal)))
            out.extend(literal)
            literal.clear()

    pos = 0
    while pos < len(data):
        lz_length = 0
        lz_distance = 0
        key = data[pos : pos + 4]
        if len(key) == 4:
            previous_positions = chains.get(key, ())
            for previous in reversed(previous_positions[-LZ_CHAIN_LIMIT:]):
                distance = pos - previous
                if distance > LZ_MAX_DISTANCE:
                    break
                limit = min(len(data) - pos, LZ_MAX_MATCH)
                length = 4
                # Overlapping matches are legal: the decoder copies one byte at a
                # time, so patterns such as "abcabcabc..." remain compact.
                while length < limit and data[previous + length] == data[pos + length]:
                    length += 1
                if length > lz_length:
                    encoded_cost = 1 + len(_encode_varint(distance)) + len(_encode_varint(length))
                    if length >= LZ_MIN_MATCH and length > encoded_cost:
                        lz_length, lz_distance = length, distance

        # Byte runs get their own token; this beats storing "aaaa..." in a dictionary.
        run = 1
        while pos + run < len(data) and data[pos + run] == data[pos] and run < 0x7FFFFFFF:
            run += 1
        dictionary_match = next(
            ((index, entry) for index, entry in by_first.get(data[pos], ()) if data.startswith(entry, pos)),
            None,
        )
        dictionary_length = len(dictionary_match[1]) if dictionary_match else 0

        if run >= 4 and run >= lz_length and run >= dictionary_length:
            flush_literal()
            out.append(2)
            out.append(data[pos])
            out.extend(_encode_varint(run))
            consumed = run
        elif lz_length >= dictionary_length and lz_length >= LZ_MIN_MATCH:
            flush_literal()
            out.append(3)
            out.extend(_encode_varint(lz_distance))
            out.extend(_encode_varint(lz_length))
            consumed = lz_length
        elif dictionary_match is not None:
            flush_literal()
            out.append(1)
            out.extend(_encode_varint(dictionary_match[0]))
            consumed = dictionary_length
        else:
            literal.append(data[pos])
            consumed = 1

        # Index consumed positions so later matches can begin anywhere, including
        # within a region that was itself encoded as one reference.
        end = min(pos + consumed, len(data))
        for indexed_pos in range(pos, end):
            indexed_key = data[indexed_pos : indexed_pos + 4]
            if len(indexed_key) == 4:
                if indexed_key not in chains and len(chains) >= LZ_MAX_HASH_KEYS:
                    # Bound memory on high-entropy input. Clearing the search
                    # index changes compression ratio only, never correctness.
                    chains.clear()
                bucket = chains.setdefault(indexed_key, [])
                bucket.append(indexed_pos)
                if len(bucket) > LZ_CHAIN_LIMIT * 2:
                    del bucket[:LZ_CHAIN_LIMIT]
        pos = end
    flush_literal()
    return bytes(out)


def compress(
    data: bytes,
    *,
    candidate_selector: Callable[[Sequence[Candidate]], Iterable[int]] | None = None,
    max_dictionary_entries: int = 128,
) -> CompressionResult:
    """Compress bytes into one self-contained .aip stream.

    ``candidate_selector`` may be backed by an LLM, but it can only choose IDs
    produced by the deterministic candidate finder. Invalid IDs are ignored.
    """
    candidates = find_candidates(data)
    selected = candidates
    used_ai = candidate_selector is not None
    if candidate_selector is not None and candidates:
        allowed = {candidate.id: candidate for candidate in candidates}
        try:
            ids = list(candidate_selector(candidates))
            selected = [allowed[value] for value in ids if type(value) is int and value in allowed]
        except Exception:
            raise
    dictionary = _remove_redundant((candidate.data for candidate in selected), max_dictionary_entries)
    tokens = _encode_tokens(data, dictionary)
    payload = tokens
    flags = 0

    header = bytearray(MAGIC)
    header.append(VERSION)
    header.append(flags)
    header.extend(_encode_varint(len(data)))
    header.extend(_encode_varint(len(dictionary)))
    for entry in dictionary:
        header.extend(_encode_varint(len(entry)))
        header.extend(entry)
    header.extend(_encode_varint(len(payload)))
    header.extend(hashlib.sha256(data).digest())
    packed = bytes(header) + payload
    return CompressionResult(
        data=packed,
        original_size=len(data),
        compressed_size=len(packed),
        dictionary_entries=len(dictionary),
        dictionary_bytes=sum(map(len, dictionary)),
        used_zlib=False,
        used_ai=used_ai,
    )


def decompress(blob: bytes, *, max_output_size: int = DEFAULT_MAX_OUTPUT) -> bytes:
    """Restore an AIP stream and verify its declared size and SHA-256 digest."""
    reader = _Reader(blob)
    if reader.take(4) != MAGIC:
        raise AIPError("not an AIP file (magic mismatch)")
    if reader.byte() != VERSION:
        raise AIPError("unsupported AIP version")
    flags = reader.byte()
    if flags & ~FLAG_ZLIB:
        raise AIPError("unsupported AIP flags")
    original_size = reader.varint()
    if original_size > max_output_size:
        raise AIPError(f"output exceeds safety limit ({max_output_size} bytes)")
    count = reader.varint()
    if count > MAX_DICTIONARY_ENTRIES:
        raise AIPError("dictionary has too many entries")
    dictionary: list[bytes] = []
    for _ in range(count):
        size = reader.varint()
        if not 4 <= size <= MAX_ENTRY_SIZE:
            raise AIPError("invalid dictionary entry size")
        dictionary.append(reader.take(size))
    payload_size = reader.varint()
    expected_hash = reader.take(32)
    payload = reader.take(payload_size)
    if reader.pos != len(blob):
        raise AIPError("unexpected bytes after AIP payload")
    if flags & FLAG_ZLIB:
        try:
            inflater = zlib.decompressobj()
            token_limit = max(1024, original_size * 2 + 1024)
            payload = inflater.decompress(payload, token_limit + 1)
            if len(payload) > token_limit or inflater.unconsumed_tail or not inflater.eof or inflater.unused_data:
                raise AIPError("inflated token stream exceeds its safety limit")
            payload += inflater.flush()
            if len(payload) > token_limit:
                raise AIPError("inflated token stream exceeds its safety limit")
        except zlib.error as exc:
            raise AIPError("invalid compressed token stream") from exc

    tokens = _Reader(payload)
    output = bytearray()
    while tokens.pos < len(payload):
        opcode = tokens.byte()
        if opcode == 0:
            size = tokens.varint()
            output.extend(tokens.take(size))
        elif opcode == 1:
            index = tokens.varint()
            if index >= len(dictionary):
                raise AIPError("dictionary reference is out of range")
            output.extend(dictionary[index])
        elif opcode == 2:
            value = tokens.byte()
            count = tokens.varint()
            if count < 4:
                raise AIPError("invalid run token")
            output.extend(bytes((value,)) * count)
        elif opcode == 3:
            distance = tokens.varint()
            length = tokens.varint()
            if distance < 1 or distance > len(output) or length < LZ_MIN_MATCH:
                raise AIPError("invalid AIP back-reference")
            if len(output) + length > original_size:
                raise AIPError("back-reference exceeds declared size")
            for _ in range(length):
                output.append(output[-distance])
        else:
            raise AIPError(f"unknown token opcode {opcode}")
        if len(output) > original_size or len(output) > max_output_size:
            raise AIPError("decoded data exceeds declared size")

    restored = bytes(output)
    if len(restored) != original_size:
        raise AIPError("decoded size does not match header")
    if hashlib.sha256(restored).digest() != expected_hash:
        raise AIPError("checksum mismatch")
    return restored
