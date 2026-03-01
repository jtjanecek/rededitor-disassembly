#!/usr/bin/env python3
"""Export/import lvmp### level-up stat entries between system_sbin.s and JSON.

This tool operates on the pksdir0 table in data/system_sbin.s and only touches
entries whose archive name matches lvmpNNN.

Export mode:
  - Reads SIRO+AT payloads from system_sbin.s
  - Decompresses AT3P/AT4P payloads
  - Decodes LevelData records into JSON files

Import mode:
  - Reads edited JSON files
  - Re-encodes LevelData records
  - Recompresses as AT4P literal streams
  - Replaces only the payload .byte blocks for lvmp entries in system_sbin.s
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


RE_LABEL = re.compile(r"^\s*([A-Za-z0-9_]+):\s*$")
RE_4BYTE = re.compile(r"^\s*\.4byte\s+([A-Za-z0-9_]+)(?:\s*@.*)?\s*$")
RE_BYTE = re.compile(r"^\s*\.byte\s+(.+?)\s*$")
RE_STRING = re.compile(r'^\s*\.string\s+"(.*)"\s*$')
RE_LVMP_NAME = re.compile(r"^lvmp(\d{3})$")

LEVEL_RECORD_SIZE = 12


@dataclass(frozen=True)
class LvmpEntry:
    archive_name: str
    species_id: int
    name_symbol: str
    data_symbol: str
    payload_symbol: str
    payload_bytes: bytes
    payload_start_line: int
    payload_end_line: int


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_int_token(token: str) -> int:
    token = token.strip()
    if not token:
        fail("Empty byte token in .byte directive")
    try:
        value = int(token, 0)
    except ValueError as exc:
        fail(f"Could not parse integer token '{token}': {exc}")
    return value & 0xFF


def parse_byte_directive(arg_text: str) -> List[int]:
    return [parse_int_token(part) for part in arg_text.split(",") if part.strip()]


def decode_asm_string(value: str) -> bytes:
    # .string in GNU as writes a trailing NUL byte.
    decoded = bytes(value, "utf-8").decode("unicode_escape").encode("latin1")
    if not decoded.endswith(b"\x00"):
        decoded += b"\x00"
    return decoded


def build_label_index(lines: List[str]) -> Dict[str, int]:
    labels: Dict[str, int] = {}
    for idx, line in enumerate(lines):
        match = RE_LABEL.match(line)
        if match:
            labels[match.group(1)] = idx
    return labels


def find_line(lines: List[str], needle: str) -> int:
    for idx, line in enumerate(lines):
        if needle in line:
            return idx
    fail(f"Could not find line containing '{needle}'")


def parse_root_table_pairs(lines: List[str]) -> List[Tuple[str, str]]:
    table_start = find_line(lines, "DataRootTable:")
    table_end = find_line(lines, "@ End of Data Root Table")
    symbols: List[str] = []

    for idx in range(table_start + 1, table_end):
        match = RE_4BYTE.match(lines[idx])
        if match:
            symbols.append(match.group(1))

    if len(symbols) % 2 != 0:
        fail("DataRootTable has an odd number of .4byte symbols")

    pairs: List[Tuple[str, str]] = []
    for i in range(0, len(symbols), 2):
        pairs.append((symbols[i], symbols[i + 1]))
    return pairs


def parse_symbol_bytes(lines: List[str], labels: Dict[str, int], symbol: str) -> bytes:
    if symbol not in labels:
        fail(f"Symbol '{symbol}' not found")

    idx = labels[symbol] + 1
    out = bytearray()

    while idx < len(lines):
        line = lines[idx]
        if RE_LABEL.match(line) or line.lstrip().startswith(".global"):
            break

        stripped = line.strip()
        if not stripped or stripped.startswith("@"):
            idx += 1
            continue

        byte_match = RE_BYTE.match(line)
        if byte_match:
            out.extend(parse_byte_directive(byte_match.group(1)))
            idx += 1
            continue

        str_match = RE_STRING.match(line)
        if str_match:
            out.extend(decode_asm_string(str_match.group(1)))
            idx += 1
            continue

        break

    return bytes(out)


def c_string(data: bytes) -> str:
    end = data.find(b"\x00")
    if end == -1:
        end = len(data)
    return data[:end].decode("latin1")


def get_payload_symbol(lines: List[str], labels: Dict[str, int], data_symbol: str) -> str:
    if data_symbol not in labels:
        fail(f"Data symbol '{data_symbol}' not found")

    idx = labels[data_symbol] + 1
    while idx < len(lines):
        line = lines[idx]
        if RE_LABEL.match(line):
            break

        match = RE_4BYTE.match(line)
        if match:
            return match.group(1)

        idx += 1

    fail(f"Could not find payload .4byte reference for '{data_symbol}'")


def get_payload_block(lines: List[str], labels: Dict[str, int], payload_symbol: str) -> Tuple[int, int, bytes]:
    if payload_symbol not in labels:
        fail(f"Payload symbol '{payload_symbol}' not found")

    idx = labels[payload_symbol] + 1

    while idx < len(lines) and not RE_BYTE.match(lines[idx]):
        stripped = lines[idx].strip()
        if RE_LABEL.match(lines[idx]) or lines[idx].lstrip().startswith(".global"):
            fail(f"No payload .byte block found for '{payload_symbol}'")
        if stripped and not stripped.startswith("@"):
            fail(f"Unexpected directive in payload block for '{payload_symbol}': {lines[idx].rstrip()}")
        idx += 1

    start = idx
    out = bytearray()

    while idx < len(lines):
        match = RE_BYTE.match(lines[idx])
        if not match:
            break
        out.extend(parse_byte_directive(match.group(1)))
        idx += 1

    end = idx
    if start == end:
        fail(f"Empty payload .byte block for '{payload_symbol}'")

    return start, end, bytes(out)


def decompress_at(data: bytes) -> bytes:
    if len(data) < 0x12:
        fail("AT payload too short")

    magic = data[0:4]
    if magic == b"AT4P":
        idx_start = 0x12
    elif magic == b"AT3P":
        idx_start = 0x10
    else:
        fail("Unsupported payload: expected AT3P/AT4P")

    compressed_length = data[5] | (data[6] << 8)
    if compressed_length > len(data):
        fail("AT payload length field exceeds available bytes")

    mode = data[4]
    if mode == ord("N"):
        end = 7 + compressed_length
        if end > len(data):
            fail("AT N-mode payload length exceeds available bytes")
        return bytes(data[7:end])

    flags = [data[7 + i] + 3 for i in range(9)]
    cur = idx_start
    out = bytearray()
    cmd_bit = 8
    current_byte = 0

    while cur < compressed_length:
        if cmd_bit == 8:
            current_byte = data[cur]
            cur += 1
            cmd_bit = 0

        if (current_byte & 0x80) == 0:
            if cur >= compressed_length:
                fail("Unexpected end of AT stream while reading command")
            command = (data[cur] >> 4) + 3
            tmp = (data[cur] & 0x0F) << 8

            if command == flags[0]:
                command = 0x1F
            elif command == flags[1]:
                command = 0x1E
            elif command == flags[2]:
                command = 0x1D
            elif command == flags[3]:
                command = 0x1C
            elif command == flags[4]:
                command = 0x1B
            elif command == flags[5]:
                command = 0x1A
            elif command == flags[6]:
                command = 0x19
            elif command == flags[7]:
                command = 0x18
            elif command == flags[8]:
                command = 0x17

            if command == 0x1F:  # aaaa
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
            elif command == 0x1E:  # abbb
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c + 1) & 0xF))
                out.append(((c + 1) & 0xF) << 4 | ((c + 1) & 0xF))
            elif command == 0x1D:  # babb
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c - 1) & 0xF))
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
            elif command == 0x1C:  # bbab
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
                out.append(((c - 1) & 0xF) << 4 | ((c + 0) & 0xF))
            elif command == 0x1B:  # bbba
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
                out.append(((c + 0) & 0xF) << 4 | ((c - 1) & 0xF))
            elif command == 0x1A:  # baaa
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c - 1) & 0xF))
                out.append(((c - 1) & 0xF) << 4 | ((c - 1) & 0xF))
            elif command == 0x19:  # abaa
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c + 1) & 0xF))
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
            elif command == 0x18:  # aaba
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
                out.append(((c + 1) & 0xF) << 4 | ((c + 0) & 0xF))
            elif command == 0x17:  # aaab
                c = data[cur] & 0x0F
                cur += 1
                out.append(((c + 0) & 0xF) << 4 | ((c + 0) & 0xF))
                out.append(((c + 0) & 0xF) << 4 | ((c + 1) & 0xF))
            else:
                if cur + 1 >= compressed_length:
                    fail("Unexpected end of AT stream while reading backref")
                # Mirrors src[curIndex++, curIndex++] from the game: consume two
                # bytes and use the second one as the low 8 bits.
                tmp += data[cur + 1]
                cur += 2
                tmp += len(out) - 0x1000
                if tmp < 0:
                    fail("AT stream backref points before output start")
                for _ in range(command):
                    if tmp >= len(out):
                        fail("AT stream backref points past output end")
                    out.append(out[tmp])
                    tmp += 1
        else:
            if cur >= compressed_length:
                fail("Unexpected end of AT stream while reading literal")
            out.append(data[cur])
            cur += 1

        cmd_bit += 1
        current_byte = (current_byte << 1) & 0xFF

    return bytes(out)


def compress_at4p_literal(raw: bytes) -> bytes:
    if len(raw) > 0xFFFF:
        fail("Raw level data too large for AT4P 16-bit length field")

    stream = bytearray()
    pos = 0
    while pos < len(raw):
        chunk = raw[pos:pos + 8]
        stream.append(0xFF)
        stream.extend(chunk)
        pos += len(chunk)

    total_len = 0x12 + len(stream)
    if total_len > 0xFFFF:
        fail("Compressed AT4P stream too large for 16-bit length field")

    header = bytearray(0x12)
    header[0:4] = b"AT4P"
    header[4] = ord("X")
    header[5] = total_len & 0xFF
    header[6] = (total_len >> 8) & 0xFF
    header[0x10] = len(raw) & 0xFF
    header[0x11] = (len(raw) >> 8) & 0xFF

    return bytes(header + stream)


def decode_level_records(raw: bytes) -> List[dict]:
    if len(raw) % LEVEL_RECORD_SIZE != 0:
        fail(f"Decoded level data size {len(raw)} is not divisible by {LEVEL_RECORD_SIZE}")

    records: List[dict] = []
    count = len(raw) // LEVEL_RECORD_SIZE
    for i in range(count):
        offset = i * LEVEL_RECORD_SIZE
        exp_required, gain_hp, gain_atk, gain_sp_atk, gain_def, gain_sp_def, fill_a = struct.unpack_from(
            "<iHBBBBH", raw, offset
        )
        records.append(
            {
                "level": i + 1,
                "expRequired": exp_required,
                "gainHP": gain_hp,
                "gainAtk": gain_atk,
                "gainSpAtk": gain_sp_atk,
                "gainDef": gain_def,
                "gainSpDef": gain_sp_def,
                "fillA": fill_a,
            }
        )
    return records


def encode_level_records(records: List[dict], archive_name: str) -> bytes:
    if not records:
        fail(f"{archive_name}: no records provided")

    out = bytearray()
    for i, entry in enumerate(records):
        level = int(entry.get("level", i + 1))
        if level != i + 1:
            fail(f"{archive_name}: expected level {i + 1}, found level {level}")

        exp_required = int(entry["expRequired"])
        gain_hp = int(entry["gainHP"])
        gain_atk = int(entry["gainAtk"])
        gain_sp_atk = int(entry["gainSpAtk"])
        gain_def = int(entry["gainDef"])
        gain_sp_def = int(entry["gainSpDef"])
        fill_a = int(entry.get("fillA", 0))

        out.extend(struct.pack("<iHBBBBH", exp_required, gain_hp, gain_atk, gain_sp_atk, gain_def, gain_sp_def, fill_a))

    return bytes(out)


def render_byte_lines(data: bytes, bytes_per_line: int = 16) -> List[str]:
    lines: List[str] = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i:i + bytes_per_line]
        lines.append("\t.byte " + ", ".join(f"0x{b:02x}" for b in chunk) + "\n")
    return lines


def load_lvmp_entries(system_sbin_path: Path) -> Tuple[List[str], List[LvmpEntry]]:
    lines = system_sbin_path.read_text(encoding="utf-8").splitlines(keepends=True)
    labels = build_label_index(lines)

    root_pairs = parse_root_table_pairs(lines)

    entries: List[LvmpEntry] = []
    for name_symbol, data_symbol in root_pairs:
        name_value = c_string(parse_symbol_bytes(lines, labels, name_symbol))
        match = RE_LVMP_NAME.match(name_value)
        if not match:
            continue

        species_id = int(match.group(1))
        payload_symbol = get_payload_symbol(lines, labels, data_symbol)
        payload_start, payload_end, payload_bytes = get_payload_block(lines, labels, payload_symbol)

        entries.append(
            LvmpEntry(
                archive_name=name_value,
                species_id=species_id,
                name_symbol=name_symbol,
                data_symbol=data_symbol,
                payload_symbol=payload_symbol,
                payload_bytes=payload_bytes,
                payload_start_line=payload_start,
                payload_end_line=payload_end,
            )
        )

    entries.sort(key=lambda e: e.species_id)

    if not entries:
        fail("No lvmp entries found in DataRootTable")

    return lines, entries


def export_lvmp(system_sbin: Path, out_dir: Path) -> None:
    _, entries = load_lvmp_entries(system_sbin)
    out_dir.mkdir(parents=True, exist_ok=True)

    index = []
    for entry in entries:
        raw = decompress_at(entry.payload_bytes)
        records = decode_level_records(raw)
        output_obj = {
            "archiveName": entry.archive_name,
            "speciesId": entry.species_id,
            "recordCount": len(records),
            "records": records,
        }

        output_path = out_dir / f"{entry.archive_name}.json"
        output_path.write_text(json.dumps(output_obj, indent=2) + "\n", encoding="utf-8")

        index.append(
            {
                "archiveName": entry.archive_name,
                "speciesId": entry.species_id,
                "nameSymbol": entry.name_symbol,
                "dataSymbol": entry.data_symbol,
                "payloadSymbol": entry.payload_symbol,
            }
        )

    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(entries)} lvmp JSON files to {out_dir}")


def load_json_records(path: Path) -> List[dict]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        records = obj.get("records")
        if isinstance(records, list):
            return records
    fail(f"{path}: expected either a list of records or an object with 'records'")


def import_lvmp(system_sbin: Path, in_dir: Path, dry_run: bool) -> None:
    lines, entries = load_lvmp_entries(system_sbin)

    by_payload: Dict[str, List[LvmpEntry]] = {}
    for entry in entries:
        by_payload.setdefault(entry.payload_symbol, []).append(entry)

    replacements: List[Tuple[int, int, List[str]]] = []
    changed_payloads = 0
    changed_names: List[str] = []

    for payload_symbol, group in by_payload.items():
        base_entry = group[0]
        base_raw = decompress_at(base_entry.payload_bytes)

        selected_raw: bytes | None = None
        selected_from: str | None = None

        for entry in group:
            json_path = in_dir / f"{entry.archive_name}.json"
            if not json_path.exists():
                continue

            records = load_json_records(json_path)
            candidate_raw = encode_level_records(records, entry.archive_name)

            if selected_raw is None:
                selected_raw = candidate_raw
                selected_from = entry.archive_name
            elif selected_raw != candidate_raw:
                fail(
                    f"Conflicting edits for shared payload '{payload_symbol}' between "
                    f"{selected_from} and {entry.archive_name}"
                )

        if selected_raw is None:
            continue

        if selected_raw == base_raw:
            continue

        encoded_payload = compress_at4p_literal(selected_raw)
        replacements.append(
            (base_entry.payload_start_line, base_entry.payload_end_line, render_byte_lines(encoded_payload))
        )
        changed_payloads += 1
        changed_names.extend(entry.archive_name for entry in group)

    if not replacements:
        print("No lvmp payload changes detected.")
        return

    # Apply from bottom to top so line indices stay valid.
    for start, end, new_block in sorted(replacements, key=lambda item: item[0], reverse=True):
        lines[start:end] = new_block

    if dry_run:
        print(f"Dry-run: would update {changed_payloads} payload blocks ({len(changed_names)} lvmp names).")
        return

    system_sbin.write_text("".join(lines), encoding="utf-8")
    print(f"Updated {changed_payloads} payload blocks in {system_sbin} ({len(changed_names)} lvmp names).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export/import lvmp level-up stats between data/system_sbin.s and JSON files"
    )
    parser.add_argument(
        "mode",
        choices=["export", "import"],
        help="export: dump lvmp JSON files, import: patch lvmp payloads from JSON",
    )
    parser.add_argument(
        "--system-sbin",
        default="data/system_sbin.s",
        help="Path to system_sbin.s (default: data/system_sbin.s)",
    )
    parser.add_argument(
        "--dir",
        default="data/monster/levelup",
        help="Directory for lvmp JSON files (default: data/monster/levelup)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Import only: show whether changes would be written",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    system_sbin = Path(args.system_sbin)
    if not system_sbin.exists():
        fail(f"system_sbin path does not exist: {system_sbin}")

    json_dir = Path(args.dir)

    if args.mode == "export":
        export_lvmp(system_sbin, json_dir)
    else:
        if not json_dir.exists():
            fail(f"JSON directory does not exist: {json_dir}")
        import_lvmp(system_sbin, json_dir, args.dry_run)


if __name__ == "__main__":
    main()
