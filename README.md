# AI Package

> An experimental lossless compression format that combines deterministic binary pattern detection with optional AI-assisted dictionary selection.

[한국어](README.ko.md)

AI Package creates self-contained `.aip` archives containing compressed data, a reusable byte-pattern dictionary, file metadata, and a SHA-256 checksum. It supports multiple files without wrapping them in ZIP or another archive format.

## Getting Started

### Requirements

- Python 3.10 or later
- Optional: [Ollama](https://ollama.com/) for local LLM-assisted candidate selection

### Run without installation

```bash
git clone <repository-url>
cd aip
python3 -m aip.cli serve --open
```

The browser UI runs at `http://127.0.0.1:8765`.

### CLI

```bash
# Built-in algorithm
aip compress sample.bin

# Local Ollama model
aip compress sample.bin --ai --model qwen2.5-coder:7b

# Restore and verify SHA-256
aip decompress sample.bin.aip restored.bin
```

The web UI additionally supports multiple files, external AI APIs, drag-and-drop, Korean/English display, upload progress, and comparison against the original files after decompression.

## Compression Methods

AI Package always performs encoding and decoding with deterministic AIP code. AI never creates the compressed payload and can only select IDs from byte-pattern candidates already discovered and validated by the algorithm.

The compression pipeline is:

1. Read the input as raw bytes. Multiple files are first combined into an AIP bundle containing file names, sizes, and contents.
2. Find repeated byte sequences at several fixed lengths and calculate how often each sequence appears.
3. Estimate the saving after subtracting dictionary storage and reference-token overhead.
4. Select a dictionary with the built-in algorithm, Ollama, or an external AI API.
5. Encode the complete input from beginning to end using the smallest applicable token.
6. Write the header, dictionary, token stream, original size, and SHA-256 checksum into one `.aip` file.

### Token Encoding

The encoded payload uses four native token types:

- **Literal block** stores bytes that are not profitable to reference.
- **Dictionary reference** replaces a globally repeated byte sequence with its dictionary index.
- **Run-length token** stores one byte and its repetition count.
- **Back-reference** stores the distance and length of a byte sequence that already appeared earlier.

The encoder compares available matches and normally chooses the longest useful representation. The decoder processes tokens in order and reconstructs the exact original byte stream before verifying its SHA-256 checksum.

#### Encoding Example

Consider this simplified input:

```text
HEADER|abcabcabcabc|HEADER|HEADER|
```

An illustrative dictionary could be:

```text
0 = "HEADER|"
```

The payload can then be represented conceptually as:

```text
DICT(0)
LITERAL("abc")
BACK_REFERENCE(distance=3, length=9)
DICT(0)
DICT(0)
```

`abcabcabcabc` is encoded as one literal `abc` followed by a back-reference that repeats the earlier three-byte pattern. This notation explains the behavior; the actual `.aip` file stores compact binary opcodes and variable-length integers.

### Built-in Algorithm

No AI or network request is used.

Candidate patterns are ranked by estimated byte saving. Patterns that are too short, unprofitable, duplicated, or fully contained in a stronger entry are removed. The resulting dictionary is combined with run-length and back-reference matching during tokenization.

Example:

```text
Input candidates:
ID 0: "customer_id"  length=11  occurrences=120  estimated_saving=945
ID 1: "customer"     length=8   occurrences=120  estimated_saving=590
ID 2: "xyz"          length=3   occurrences=2    estimated_saving=-4

Selected:
ID 0
```

ID 1 is redundant because it is contained in the stronger ID 0 entry. ID 2 costs more to store than it saves.

### Local LLM with Ollama

The deterministic scanner still finds every candidate first. The local model receives candidate IDs, lengths, occurrence counts, estimated savings, and Base64-encoded candidate bytes. It analyzes relationships such as overlap and redundancy, then returns only:

```json
{"selected_ids":[1,2,3]}
```

The application rejects unknown IDs and falls back to the built-in algorithm if Ollama is unavailable or returns an invalid response. Ollama connections are restricted to `localhost`, `127.0.0.1`, and `::1`.

Example:

```text
AIP scanner -> candidates [0, 1, 2, 3, 4]
Ollama      -> {"selected_ids":[0,3]}
AIP encoder -> validates IDs 0 and 3, builds the dictionary, and encodes the file
```

Even if the model returns an invented ID such as `999`, the encoder ignores it.

### External AI API

The web UI accepts an HTTP method, chat endpoint URL, headers, and body template. Put `{{data}}` where the validated candidate-selection request should be inserted.

```json
{
  "model": "your-model",
  "stream": false,
  "messages": [
    {
      "role": "user",
      "content": "{{data}}"
    }
  ]
}
```

Responses may contain `selected_ids` directly, inside a chat response string, or inside a fenced `json` code block. Invalid responses automatically fall back to the built-in algorithm.

Example response formats:

```json
{"selected_ids":[0,3,7]}
```

````text
```json
{"selected_ids":[0,3,7]}
```
````

```json
{
  "choices": [
    {
      "message": {
        "content": "{\"selected_ids\":[0,3,7]}"
      }
    }
  ]
}
```

`{{data}}` contains the candidate-selection instructions and candidate dataset, not permission for the API to produce arbitrary compressed bytes. The returned IDs are validated before use.

### Archive and Integrity Behavior

- Multiple files are stored in the native AIP bundle format, not ZIP.
- Decompression restores each original file directly.
- Optional original-file comparison uses SHA-256.
- New archives do not use ZIP or DEFLATE.
- Legacy experimental AIP files using the old DEFLATE flag remain readable.

Example multi-file archive:

```text
photos/logo.png
docs/readme.txt
data/sample.bin
        │
        ▼
Native AIP bundle
        │
        ▼
Dictionary + token stream + SHA-256
        │
        ▼
project-files.aip
```

## Benchmark

Benchmark methodology and results will be added after testing with representative real-world datasets and comparable compression tools.
