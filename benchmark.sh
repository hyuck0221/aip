#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly AI_API_URL="${AI_API_URL}"
readonly WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aip-benchmark.XXXXXX")"

cleanup() {
  rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT INT TERM

print_file_menu() {
  cat <<'EOF'
  1) 작은 TXT (약 30자)
  2) 대형 TXT (약 10 MiB)
  3) 작은 PDF
  4) 대형 PDF (약 10 MiB)
  5) 작은 Excel
  6) 대형 Excel (약 10 MiB)
EOF
}

choose_detailed_options() {
  local scope model_choice custom_model

  printf '\n[옵션 1] 테스트 파일 범위\n'
  printf '  1) 모든 파일\n  2) 선택한 파일만\n'
  read -r -p '선택 [1]: ' scope || scope=""
  scope="${scope:-1}"
  case "$scope" in
    1)
      SELECTED_FILES="1,2,3,4,5,6"
      ;;
    2)
      print_file_menu
      read -r -p '파일 번호를 쉼표로 입력 (예: 1,3,5): ' SELECTED_FILES || SELECTED_FILES=""
      if [[ ! "$SELECTED_FILES" =~ ^[[:space:]]*[1-6]([[:space:]]*,[[:space:]]*[1-6])*[[:space:]]*$ ]]; then
        printf '오류: 1~6 사이 번호를 쉼표로 구분해 입력해 주세요.\n' >&2
        exit 2
      fi
      SELECTED_FILES="${SELECTED_FILES//[[:space:]]/}"
      ;;
    *)
      printf '오류: 올바른 파일 범위 옵션을 선택해 주세요.\n' >&2
      exit 2
      ;;
  esac

  printf '\n[옵션 2] AI 모델\n'
  printf '  1) gemini-3.5-flash-medium\n  2) gemini-3.5-flash-high\n  3) 직접 입력\n'
  read -r -p '선택 [1]: ' model_choice || model_choice=""
  model_choice="${model_choice:-1}"
  case "$model_choice" in
    1) AI_MODEL="gemini-3.5-flash-medium" ;;
    2) AI_MODEL="gemini-3.5-flash-high" ;;
    3)
      read -r -p '모델명: ' custom_model || custom_model=""
      if [[ -z "${custom_model//[[:space:]]/}" ]]; then
        printf '오류: 모델명은 비워둘 수 없습니다.\n' >&2
        exit 2
      fi
      AI_MODEL="$custom_model"
      ;;
    *)
      printf '오류: 올바른 모델 옵션을 선택해 주세요.\n' >&2
      exit 2
      ;;
  esac
}

printf 'AIP 압축 벤치마크\n'
printf '  1) 기본 - 작은/대형 TXT, gemini-3.5-flash-medium\n'
printf '  2) 상세 - 파일 및 AI 모델 직접 선택\n'
read -r -p '실행 옵션 [1]: ' RUN_MODE || RUN_MODE=""
RUN_MODE="${RUN_MODE:-1}"

case "$RUN_MODE" in
  1)
    SELECTED_FILES="1,2"
    AI_MODEL="gemini-3.5-flash-medium"
    ;;
  2)
    choose_detailed_options
    ;;
  *)
    printf '오류: 1 또는 2를 선택해 주세요.\n' >&2
    exit 2
    ;;
esac

printf '\n설정: 파일=%s, 모델=%s, API=%s\n' "$SELECTED_FILES" "$AI_MODEL" "$AI_API_URL"
printf '테스트 파일 생성 및 벤치마크를 시작합니다. 임시 파일은 종료 시 삭제됩니다.\n\n'

cd "$PROJECT_ROOT"
PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 - "$WORK_DIR" "$SELECTED_FILES" "$AI_MODEL" "$AI_API_URL" <<'PY'
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import time
import zipfile

from aip.codec import compress, decompress
from aip.external_ai import ExternalAIError, select_candidates_via_api


work_dir = Path(sys.argv[1])
selected_ids = list(dict.fromkeys(int(value) for value in sys.argv[2].split(",")))
model = sys.argv[3]
api_url = sys.argv[4]
MiB = 1024 * 1024


def write_pdf(path: Path, *, large: bool) -> None:
    content = b"BT /F1 18 Tf 72 760 Td (AIP benchmark PDF) Tj ET\n"
    if large:
        line = b"% AIP benchmark repeatable PDF payload 0123456789abcdef\n"
        content += (line * ((10 * MiB - len(content) + len(line) - 1) // len(line)))[
            : 10 * MiB - len(content)
        ]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(output)


def write_xlsx(path: Path, *, large: bool) -> None:
    rows = 1 if not large else 145_000
    content_types = b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>'''
    root_rels = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    workbook = b'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Benchmark" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    workbook_rels = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>'''
    sheet = io.BytesIO()
    sheet.write(b'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
    for row in range(1, rows + 1):
        sheet.write(
            f'<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>AIP-{row % 1000:03d}</t></is></c></row>'.encode()
        )
    sheet.write(b"</sheetData></worksheet>")
    compression = zipfile.ZIP_STORED if large else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet.getvalue())


def create_samples() -> dict[int, tuple[str, Path]]:
    samples = {
        1: ("작은 TXT", work_dir / "small.txt"),
        2: ("대형 TXT", work_dir / "large.txt"),
        3: ("작은 PDF", work_dir / "small.pdf"),
        4: ("대형 PDF", work_dir / "large.pdf"),
        5: ("작은 Excel", work_dir / "small.xlsx"),
        6: ("대형 Excel", work_dir / "large.xlsx"),
    }
    for sample_id in selected_ids:
        path = samples[sample_id][1]
        if sample_id == 1:
            path.write_text("AIP benchmark small text file.", encoding="utf-8")
        elif sample_id == 2:
            line = b"AIP benchmark text: deterministic compression test 0123456789abcdef\n"
            with path.open("wb") as output:
                full, rest = divmod(10 * MiB, len(line))
                output.write(line * full)
                output.write(line[:rest])
        elif sample_id in (3, 4):
            write_pdf(path, large=sample_id == 4)
        else:
            write_xlsx(path, large=sample_id == 6)
    return samples


def human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < MiB:
        return f"{size / 1024:.2f} KiB"
    return f"{size / MiB:.2f} MiB"


def throughput(size: int, seconds: float) -> str:
    return f"{size / MiB / seconds:.2f} MiB/s" if seconds > 0 else "-"


samples = create_samples()
results = []

for position, sample_id in enumerate(selected_ids, 1):
    label, path = samples[sample_id]
    original = path.read_bytes()
    print(f"[{position}/{len(selected_ids)}] {label} 압축 중...", flush=True)
    ai_status = f"AI ({model})"
    ai_called = False

    def selector(candidates):
        nonlocal_marker[0] = True
        return select_candidates_via_api(
            candidates,
            method="POST",
            url=api_url,
            headers={"Content-Type": "application/json"},
            body_template=(
                '{"model":' + json.dumps(model) +
                ',"stream":false,"messages":[{"role":"user","content":"{{data}}"}]}'
            ),
            timeout=90.0,
        )

    nonlocal_marker = [ai_called]
    started = time.perf_counter()
    try:
        packed = compress(original, candidate_selector=selector)
    except ExternalAIError as exc:
        packed = compress(original)
        ai_status = f"알고리즘 폴백 ({exc})"
    else:
        if not nonlocal_marker[0]:
            ai_status = "AI 호출 없음 (유효 후보 없음)"
    compression_seconds = time.perf_counter() - started
    print(
        f"    압축 완료: {human_bytes(len(original))} -> {human_bytes(len(packed.data))} "
        f"({compression_seconds:.3f}s)",
        flush=True,
    )

    print(f"    압축 해제 및 SHA-256 비교 중...", flush=True)
    started = time.perf_counter()
    restored = decompress(packed.data)
    decompression_seconds = time.perf_counter() - started
    verified = hashlib.sha256(original).digest() == hashlib.sha256(restored).digest()
    print(f"    검증 완료: {'동일' if verified else '불일치'} ({decompression_seconds:.3f}s)", flush=True)

    # --- ZIP (DEFLATE) 벤치마크 ---
    print(f"    ZIP 압축 중...", flush=True)
    zip_path = path.with_suffix(path.suffix + ".zip")
    started = time.perf_counter()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(path.name, original)
    zip_compress_seconds = time.perf_counter() - started
    zip_size = zip_path.stat().st_size
    print(
        f"    ZIP 압축 완료: {human_bytes(len(original))} -> {human_bytes(zip_size)} "
        f"({zip_compress_seconds:.3f}s)",
        flush=True,
    )

    print(f"    ZIP 압축 해제 및 검증 중...", flush=True)
    started = time.perf_counter()
    with zipfile.ZipFile(zip_path, "r") as zf:
        zip_restored = zf.read(path.name)
    zip_decompress_seconds = time.perf_counter() - started
    zip_verified = hashlib.sha256(original).digest() == hashlib.sha256(zip_restored).digest()
    print(f"    ZIP 검증 완료: {'동일' if zip_verified else '불일치'} ({zip_decompress_seconds:.3f}s)", flush=True)

    results.append(
        {
            "label": label,
            "original": len(original),
            "compressed": len(packed.data),
            "ratio": len(packed.data) / len(original) * 100 if original else 0,
            "saving": (1 - len(packed.data) / len(original)) * 100 if original else 0,
            "compress_time": compression_seconds,
            "decompress_time": decompression_seconds,
            "compress_speed": throughput(len(original), compression_seconds),
            "decompress_speed": throughput(len(original), decompression_seconds),
            "dictionary": packed.dictionary_entries,
            "ai": ai_status,
            "verified": verified,
            "zip_size": zip_size,
            "zip_ratio": zip_size / len(original) * 100 if original else 0,
            "zip_saving": (1 - zip_size / len(original)) * 100 if original else 0,
            "zip_compress_time": zip_compress_seconds,
            "zip_decompress_time": zip_decompress_seconds,
            "zip_compress_speed": throughput(len(original), zip_compress_seconds),
            "zip_decompress_speed": throughput(len(original), zip_decompress_seconds),
            "zip_verified": zip_verified,
        }
    )

print("\n" + "=" * 96)
print("최종 벤치마크 결과 (AIP vs ZIP)")
print("=" * 96)
for result in results:
    print(f"\n[{result['label']}]")
    print("  ── AIP ──")
    print(
        f"  크기          : {human_bytes(result['original'])} -> {human_bytes(result['compressed'])}"
        f"  (압축률 {result['ratio']:.2f}%, 절감 {result['saving']:.2f}%)"
    )
    print(
        f"  압축          : {result['compress_time']:.3f}s, {result['compress_speed']}, "
        f"사전 {result['dictionary']}개"
    )
    print(
        f"  압축 해제     : {result['decompress_time']:.3f}s, {result['decompress_speed']}"
    )
    print(f"  AI 선택       : {result['ai']}")
    print(f"  SHA-256 비교  : {'PASS (원본과 동일)' if result['verified'] else 'FAIL (불일치)'}")
    print("  ── ZIP (DEFLATE) ──")
    print(
        f"  크기          : {human_bytes(result['original'])} -> {human_bytes(result['zip_size'])}"
        f"  (압축률 {result['zip_ratio']:.2f}%, 절감 {result['zip_saving']:.2f}%)"
    )
    print(
        f"  압축          : {result['zip_compress_time']:.3f}s, {result['zip_compress_speed']}"
    )
    print(
        f"  압축 해제     : {result['zip_decompress_time']:.3f}s, {result['zip_decompress_speed']}"
    )
    print(f"  SHA-256 비교  : {'PASS (원본과 동일)' if result['zip_verified'] else 'FAIL (불일치)'}")
    print("  ── 비교 ──")
    diff = result["compressed"] - result["zip_size"]
    if diff < 0:
        print(f"  AIP vs ZIP    : AIP가 {human_bytes(-diff)} 작음 (AIP 우세)")
    elif diff > 0:
        print(f"  AIP vs ZIP    : ZIP이 {human_bytes(diff)} 작음 (ZIP 우세)")
    else:
        print(f"  AIP vs ZIP    : 동일 크기")

total_original = sum(item["original"] for item in results)
total_compressed = sum(item["compressed"] for item in results)
total_zip = sum(item["zip_size"] for item in results)
total_compress_time = sum(item["compress_time"] for item in results)
total_decompress_time = sum(item["decompress_time"] for item in results)
total_zip_compress_time = sum(item["zip_compress_time"] for item in results)
total_zip_decompress_time = sum(item["zip_decompress_time"] for item in results)
all_verified = all(item["verified"] for item in results) and all(item["zip_verified"] for item in results)
print("\n[합계]")
print(
    f"  AIP 크기      : {human_bytes(total_original)} -> {human_bytes(total_compressed)} "
    f"(압축률 {total_compressed / total_original * 100:.2f}%)"
)
print(
    f"  ZIP 크기      : {human_bytes(total_original)} -> {human_bytes(total_zip)} "
    f"(압축률 {total_zip / total_original * 100:.2f}%)"
)
print(f"  AIP 압축 시간 : {total_compress_time:.3f}s")
print(f"  ZIP 압축 시간 : {total_zip_compress_time:.3f}s")
print(f"  AIP 해제 시간 : {total_decompress_time:.3f}s")
print(f"  ZIP 해제 시간 : {total_zip_decompress_time:.3f}s")
print(f"  무결성        : {'전체 PASS' if all_verified else 'FAIL 포함'}")
print("=" * 96)

if not all_verified:
    raise SystemExit(1)
PY
