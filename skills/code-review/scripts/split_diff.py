#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys


def extract_diff_from_json(json_data, key=None):
    if key:
        return json_data.get(key, "")
    if isinstance(json_data, str):
        return json_data
    # Try common keys
    for k in ["diff", "patch", "content"]:
        if k in json_data:
            return json_data[k]
    raise ValueError("Could not find diff in JSON data")


def _split_into_file_diffs(diff_content):
    files = re.split(r"^(?=diff --git )", diff_content, flags=re.MULTILINE)
    if len(files) <= 1:
        files = re.split(r"^(?=--- )", diff_content, flags=re.MULTILINE)
    return [f for f in files if f.strip()]


def _extract_file_name(file_diff, index):
    match = re.search(
        r'^diff --git (?:a/([^\s"]+)|"a/([^"]+)")', file_diff, re.MULTILINE
    )
    if not match:
        match = re.search(
            r'^--- (?:a/([^\t\n"]+)|"a/([^"]+)")', file_diff, re.MULTILINE
        )
    if match:
        return (match.group(1) or match.group(2)).strip()
    return f"chunk_{index}.diff"


def _byte_len(text):
    return len(text.encode("utf-8"))


def _line_count(text):
    return len(text.splitlines())


def _fits_limits(text, max_bytes, max_lines):
    return _byte_len(text) <= max_bytes and _line_count(text) <= max_lines


def _split_by_lines(header, body_text, max_bytes, max_lines):
    segments = []
    current_lines = []
    for line in body_text.splitlines(keepends=True):
        candidate = header + "".join(current_lines) + line
        if current_lines and not _fits_limits(candidate, max_bytes, max_lines):
            segments.append(header + "".join(current_lines))
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        segments.append(header + "".join(current_lines))
    return segments


def _split_oversized_file_diff(file_diff, max_bytes, max_lines):
    if _fits_limits(file_diff, max_bytes, max_lines):
        return [file_diff]

    parts = re.split(r"^(?=@@ )", file_diff, flags=re.MULTILINE)
    if len(parts) <= 1 or not parts[0].strip():
        return _split_by_lines("", file_diff, max_bytes, max_lines)

    header = parts[0]
    hunks = parts[1:]
    segments = []
    current_hunks = []

    for hunk in hunks:
        candidate = header + "".join(current_hunks) + hunk
        if not current_hunks and not _fits_limits(candidate, max_bytes, max_lines):
            segments.extend(_split_by_lines(header, hunk, max_bytes, max_lines))
        elif current_hunks and not _fits_limits(candidate, max_bytes, max_lines):
            segments.append(header + "".join(current_hunks))
            if _fits_limits(header + hunk, max_bytes, max_lines):
                current_hunks = [hunk]
            else:
                segments.extend(_split_by_lines(header, hunk, max_bytes, max_lines))
                current_hunks = []
        else:
            current_hunks.append(hunk)

    if current_hunks:
        segments.append(header + "".join(current_hunks))
    return segments


def _append_unique(items, value):
    if value not in items:
        items.append(value)


def _write_grouped_chunk(output_dir, index, texts, files):
    chunk_name = f"diff_chunk_{index:02d}.diff"
    chunk_path = os.path.join(output_dir, chunk_name)
    content = "".join(texts)
    with open(chunk_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {
        "chunk_name": chunk_name,
        "chunk_file": os.path.abspath(chunk_path),
        "files": list(files),
        "lines": _line_count(content),
        "bytes": _byte_len(content),
    }


def split_diff_grouped(
    diff_content, output_dir, max_bytes=35000, max_lines=700
):
    """Packs per-file diffs into size-bounded chunk files and writes manifest.json."""
    os.makedirs(output_dir, exist_ok=True)
    file_diffs = _split_into_file_diffs(diff_content)

    segments = []
    for i, file_diff in enumerate(file_diffs):
        file_name = _extract_file_name(file_diff, i)
        for seg in _split_oversized_file_diff(file_diff, max_bytes, max_lines):
            if not seg.endswith("\n"):
                seg += "\n"
            segments.append((file_name, seg))

    chunks_meta = []
    cur_texts = []
    cur_files = []

    for file_name, seg in segments:
        candidate = "".join(cur_texts) + seg
        if cur_texts and not _fits_limits(candidate, max_bytes, max_lines):
            chunks_meta.append(
                _write_grouped_chunk(
                    output_dir, len(chunks_meta) + 1, cur_texts, cur_files
                )
            )
            cur_texts = [seg]
            cur_files = [file_name]
        else:
            cur_texts.append(seg)
            _append_unique(cur_files, file_name)

    if cur_texts:
        chunks_meta.append(
            _write_grouped_chunk(
                output_dir, len(chunks_meta) + 1, cur_texts, cur_files
            )
        )

    manifest = {
        "total_bytes": _byte_len(diff_content),
        "total_lines": _line_count(diff_content),
        "chunk_count": len(chunks_meta),
        "chunks": chunks_meta,
    }
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return chunks_meta


def split_diff(diff_content, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    files = _split_into_file_diffs(diff_content)

    summary = []
    for i, file_diff in enumerate(files):
        file_name = _extract_file_name(file_diff, i)
        safe_name = (
            file_name.replace("/", "_")
            if file_name != f"chunk_{i}.diff"
            else file_name
        )

        file_path = os.path.join(output_dir, safe_name)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_diff)

        summary.append(f"- {file_name} -> {safe_name}")

    return summary


def _read_diff_input(args):
    content = args.input.read()
    if not args.json:
        return content
    try:
        json_data = json.loads(content)
        return extract_diff_from_json(json_data, args.json_key)
    except json.JSONDecodeError:
        print("Error: Input is not valid JSON", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Extract and split diffs for code review."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=argparse.FileType("r"),
        default=sys.stdin,
        help="Input file (default: stdin)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Input is JSON encoded"
    )
    parser.add_argument(
        "--json-key", help="Key in JSON containing the diff string"
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory to write chunks to"
    )
    parser.add_argument(
        "--grouped",
        action="store_true",
        help="Pack diffs into size-bounded diff_chunk_XX.diff files with manifest.json",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=35000,
        help="Maximum bytes per grouped chunk file (default: 35000)",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=700,
        help="Maximum lines per grouped chunk file (default: 700)",
    )

    args = parser.parse_args()
    diff_content = _read_diff_input(args)

    if args.grouped:
        chunks = split_diff_grouped(
            diff_content,
            args.output_dir,
            max_bytes=args.max_bytes,
            max_lines=args.max_lines,
        )
        print(
            f"Successfully split diff into {len(chunks)} grouped chunk(s) in {args.output_dir}"
        )
        for chunk in chunks:
            files_str = ", ".join(chunk["files"])
            print(
                f"- {chunk['chunk_file']} ({chunk['lines']} lines, {chunk['bytes']} bytes): {files_str}"
            )
        return

    summary = split_diff(diff_content, args.output_dir)
    print(f"Successfully split diff into {len(summary)} files in {args.output_dir}")
    print("\n".join(summary))


if __name__ == "__main__":
    main()
