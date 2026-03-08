#!/usr/bin/env python3
"""
RedEditor asset pipeline helper for PMD-Red projects.

This script creates and maintains `.rededitor/assets/index.json` and a mirrored
editable asset tree in `.rededitor/assets/editable`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

MANIFEST_VERSION = 1
DEFAULT_ASSETS_RELATIVE = Path(".rededitor/assets")
EDITABLE_RELATIVE = Path("editable")
PREVIEWS_RELATIVE = Path("previews")
INDEX_NAME = "index.json"

POKEMON_SOURCE_ROOT = Path("graphics/ax/mon")
OVERWORLD_SOURCE_ROOT = Path("data/map_bg")
DUNGEON_SBINS_PATH = Path("data/dungeon_sbin.s")
DUNGEON_FLOOR_ID_TABLE_PATH = Path("data/dungeon/floor_id.inc")
DUNGEON_DIR = Path("data/dungeon")
MAP_FILES_TABLE_PATH = Path("src/map_files_table.c")
DUNGEON_INFO_PATH = Path("src/dungeon_info.c")
DUNGEON_DATA_PATH = Path("data/dungeon/dungeon_data.json")

OVERWORLD_SUFFIXES = {".bpl", ".bpc", ".bma", ".bpa"}
POKEMON_EDITABLE_SUFFIXES = {".4bpp", ".pal", ".pmdpal", ".bin"}


def emit_progress(phase: str, completed: int, total: int, message: str) -> None:
    payload = {
        "phase": phase,
        "completed": int(completed),
        "total": int(total),
        "message": message,
    }
    print(json.dumps(payload), flush=True)


def emit_log(message: str) -> None:
    print(message, flush=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def files_equal(path_a: Path, path_b: Path) -> bool:
    if not path_a.exists() or not path_b.exists():
        return False
    if path_a.stat().st_size != path_b.stat().st_size:
        return False
    return file_sha256(path_a) == file_sha256(path_b)


def copy_if_changed(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and files_equal(source, destination):
        return False
    shutil.copy2(source, destination)
    return True


def seed_editable_if_missing(project_root: Path, editable_root: Path, relative_path: Path) -> bool:
    source = project_root / relative_path
    destination = editable_root / relative_path
    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def snapshot_hash(project_root: Path, relative_paths: Iterable[Path]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for relative in relative_paths:
        absolute = project_root / relative
        digest = hashlib.sha256()
        if absolute.is_file():
            st = absolute.stat()
            digest.update(str(relative).encode("utf-8"))
            digest.update(str(st.st_size).encode("utf-8"))
            digest.update(str(st.st_mtime_ns).encode("utf-8"))
        elif absolute.is_dir():
            for child in sorted(p for p in absolute.rglob("*") if p.is_file()):
                rel = child.relative_to(project_root)
                st = child.stat()
                digest.update(str(rel).encode("utf-8"))
                digest.update(str(st.st_size).encode("utf-8"))
                digest.update(str(st.st_mtime_ns).encode("utf-8"))
        else:
            digest.update(b"missing")
        result[str(relative)] = digest.hexdigest()
    return result


def parse_map_file_table(path: Path) -> List[Dict[str, object]]:
    if not path.is_file():
        return []

    content = read_text(path)
    quoted_pattern = re.compile(r'"([^"]+)"')
    table_key_pattern = re.compile(r"^\[(MAP_FILE_ID_[A-Z0-9_]+)\]\s*=\s*\{")
    parsed: List[Dict[str, object]] = []
    current: Dict[str, object] = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        table_key_match = table_key_pattern.match(line)
        if table_key_match:
            if current.get("mapId"):
                parsed.append(current)
            current = {"mapFileId": table_key_match.group(1), "files": []}
            continue
        if ".bplFileName" in line:
            names = quoted_pattern.findall(line)
            if names:
                current["mapId"] = names[0]
                current["files"] = [f"{names[0]}.bpl"]
        elif ".bpcFileName" in line and current.get("mapId"):
            names = quoted_pattern.findall(line)
            if names:
                current.setdefault("files", []).append(f"{names[0]}.bpc")
        elif ".bmaFileName" in line and current.get("mapId"):
            names = quoted_pattern.findall(line)
            if names:
                current.setdefault("files", []).append(f"{names[0]}.bma")
        elif ".bpaFileNames" in line and current.get("mapId"):
            for name in quoted_pattern.findall(line):
                current.setdefault("files", []).append(f"{name}.bpa")
        elif line.startswith("},") and current.get("mapId"):
            map_file_id = str(current.get("mapFileId", ""))
            map_id = str(current.get("mapId", ""))
            current["pokemonSquareCandidate"] = (
                "POKEMON_SQUARE" in map_file_id
                or map_id.startswith("T01P01")
                or map_id.startswith("T00P01")
            )
            parsed.append(current)
            current = {}

    if current.get("mapId"):
        map_file_id = str(current.get("mapFileId", ""))
        map_id = str(current.get("mapId", ""))
        current["pokemonSquareCandidate"] = (
            "POKEMON_SQUARE" in map_file_id
            or map_id.startswith("T01P01")
            or map_id.startswith("T00P01")
        )
        parsed.append(current)

    return parsed


def parse_dungeon_folder_order(path: Path) -> List[str]:
    if not path.is_file():
        return []
    content = read_text(path)
    include_pattern = re.compile(r'#include\s+"([^"/]+)/floor_id\.inc"')
    folders = []
    for folder in include_pattern.findall(content):
        if folder and folder not in folders:
            folders.append(folder)
    return folders


def parse_int_tokens(text: str) -> List[int]:
    values = []
    for token in re.findall(r"-?0x[0-9A-Fa-f]+|-?\d+", text):
        try:
            values.append(int(token, 0))
        except ValueError:
            continue
    return values


def parse_floor_tilesets(path: Path) -> Tuple[List[int], Optional[str]]:
    if not path.is_file():
        return [], None

    raw_values: List[int] = []
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if ".byte" not in line:
            continue
        line_values = parse_int_tokens(line)
        if line_values:
            raw_values.extend(line_values)

    if not raw_values:
        return [], None

    floor_record_size = 28
    if len(raw_values) < floor_record_size:
        return [], f"Invalid floor properties length in {path}: {len(raw_values)} bytes"

    floor_count = len(raw_values) // floor_record_size
    remainder = len(raw_values) % floor_record_size
    tilesets: List[int] = []

    for index in range(floor_count):
        start = index * floor_record_size
        record = raw_values[start : start + floor_record_size]
        if len(record) < 3:
            continue
        tileset = int(record[2]) & 0xFF
        tilesets.append(tileset)

    if remainder:
        return tilesets, f"Floor properties in {path} has trailing bytes: {remainder}"
    return tilesets, None


def parse_dungeon_tilesets(path: Path, dungeon_info_path: Path) -> Tuple[List[Dict[str, object]], List[int]]:
    if not path.is_file():
        return [], []

    content = read_text(path)
    string_pattern = re.compile(r'\.string\s+"([^"]+)"')
    name_pattern = re.compile(r"^b(\d{2})(fon|pal|cel|cex|canm|emap\d)$")

    tilesets: Dict[int, Dict[str, object]] = {}
    for value in string_pattern.findall(content):
        cleaned = value.split("\\0", 1)[0]
        match = name_pattern.match(cleaned)
        if not match:
            continue
        tileset_id = int(match.group(1))
        suffix = match.group(2)
        entry = tilesets.setdefault(
            tileset_id, {"tilesetId": tileset_id, "label": f"b{tileset_id:02d}", "files": []}
        )
        entry["files"].append(f"b{tileset_id:02d}{suffix}")

    mapped_file_ids = parse_tileset_file_remap(dungeon_info_path)

    ordered = [tilesets[key] for key in sorted(tilesets)]
    for entry in ordered:
        entry["files"] = sorted(set(entry["files"]))
    return ordered, mapped_file_ids


def parse_tileset_file_remap(path: Path) -> List[int]:
    if not path.is_file():
        return []
    content = read_text(path)
    match = re.search(r"const u8 gUnknown_8108EC0\[\]\s*=\s*\{([^}]+)\};", content, flags=re.DOTALL)
    if not match:
        return []
    values = []
    for chunk in match.group(1).replace("\n", " ").split(","):
        raw = chunk.strip()
        if not raw:
            continue
        try:
            values.append(int(raw))
        except ValueError:
            continue
    return values


def load_dungeon_names(path: Path) -> List[str]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(read_text(path))
    except Exception:
        return []
    names = []
    for entry in payload:
        if isinstance(entry, dict):
            value = entry.get("name")
            if isinstance(value, str):
                names.append(value)
    return names


def ensure_asset_directories(assets_root: Path) -> Tuple[Path, Path, Path]:
    editable_root = assets_root / EDITABLE_RELATIVE
    previews_root = assets_root / PREVIEWS_RELATIVE
    pokemon_preview_root = previews_root / "pokemon"
    dungeon_preview_root = previews_root / "dungeon"
    overworld_preview_root = previews_root / "overworld"

    editable_root.mkdir(parents=True, exist_ok=True)
    pokemon_preview_root.mkdir(parents=True, exist_ok=True)
    dungeon_preview_root.mkdir(parents=True, exist_ok=True)
    overworld_preview_root.mkdir(parents=True, exist_ok=True)
    return editable_root, previews_root, overworld_preview_root


def collect_pokemon(
    project_root: Path, editable_root: Path, previews_root: Path
) -> Tuple[Dict[str, object], List[Dict[str, object]], List[str], List[Path]]:
    pokemon_root = project_root / POKEMON_SOURCE_ROOT
    pokemon_assets: List[Dict[str, object]] = []
    preview_entries: List[Dict[str, object]] = []
    errors: List[str] = []
    editable_paths: List[Path] = []

    preview_target = previews_root / "pokemon"

    if not pokemon_root.is_dir():
        errors.append(f"Pokemon source folder missing: {pokemon_root}")
        return {"kind": "pokemon", "entries": []}, [], errors, []

    for mon_dir in sorted([p for p in pokemon_root.iterdir() if p.is_dir()]):
        relative_mon = mon_dir.relative_to(project_root)
        png_files = sorted(mon_dir.glob("*.png"))
        source_files = sorted([p for p in mon_dir.iterdir() if p.is_file()])

        for source_file in source_files:
            if source_file.suffix.lower() in POKEMON_EDITABLE_SUFFIXES:
                rel = source_file.relative_to(project_root)
                seed_editable_if_missing(project_root, editable_root, rel)
                editable_paths.append(rel)

        preview_path_abs = None
        if png_files:
            first_png = png_files[0]
            preview_path = preview_target / f"{mon_dir.name}.png"
            copy_if_changed(first_png, preview_path)
            preview_path_abs = str(preview_path.resolve())

        pokemon_assets.append(
            {
                "id": mon_dir.name,
                "sourceDir": str(relative_mon),
                "frameCount": len(png_files),
                "fileCount": len(source_files),
            }
        )

        if preview_path_abs:
            preview_entries.append(
                {
                    "kind": "pokemon",
                    "id": mon_dir.name,
                    "keys": [mon_dir.name, mon_dir.name.replace("_", ""), mon_dir.name.replace("-", "")],
                    "previewPath": preview_path_abs,
                    "frameCount": len(png_files),
                }
            )

    return {"kind": "pokemon", "entries": pokemon_assets}, preview_entries, errors, sorted(
        set(editable_paths)
    )


def collect_overworld(
    project_root: Path, editable_root: Path, previews_root: Path
) -> Tuple[Dict[str, object], List[Dict[str, object]], List[str], List[Path]]:
    map_bg_root = project_root / OVERWORLD_SOURCE_ROOT
    errors: List[str] = []
    assets: List[Dict[str, object]] = []
    previews: List[Dict[str, object]] = []
    editable_paths: List[Path] = []

    if not map_bg_root.is_dir():
        errors.append(f"Overworld map source folder missing: {map_bg_root}")
        return {"kind": "overworld", "entries": []}, [], errors, []

    for source in sorted([p for p in map_bg_root.iterdir() if p.is_file() and p.suffix.lower() in OVERWORLD_SUFFIXES]):
        rel = source.relative_to(project_root)
        seed_editable_if_missing(project_root, editable_root, rel)
        editable_paths.append(rel)

    table_entries = parse_map_file_table(project_root / MAP_FILES_TABLE_PATH)
    overview_path = previews_root / "overworld" / "map_index.txt"
    overview_lines = []
    for entry in table_entries:
        files = ", ".join(entry.get("files", []))
        overview_lines.append(f"{entry.get('mapId')}: {files}")
        assets.append(entry)
        previews.append(
            {
                "kind": "overworld_map",
                "mapFileId": entry.get("mapFileId"),
                "mapId": entry.get("mapId"),
                "files": entry.get("files", []),
                "pokemonSquareCandidate": bool(entry.get("pokemonSquareCandidate", False)),
                "summaryPath": str(overview_path.resolve()),
            }
        )

    write_text(overview_path, "\n".join(overview_lines) + ("\n" if overview_lines else ""))
    return {"kind": "overworld", "entries": assets}, previews, errors, sorted(set(editable_paths))


def collect_dungeon(
    project_root: Path, editable_root: Path, previews_root: Path
) -> Tuple[Dict[str, object], List[Dict[str, object]], List[str], List[Path]]:
    errors: List[str] = []
    editable_paths: List[Path] = []
    dungeon_sbin = project_root / DUNGEON_SBINS_PATH
    if dungeon_sbin.is_file():
        seed_editable_if_missing(project_root, editable_root, DUNGEON_SBINS_PATH)
        editable_paths.append(DUNGEON_SBINS_PATH)
    else:
        errors.append(f"Dungeon archive source missing: {dungeon_sbin}")

    tilesets, remap = parse_dungeon_tilesets(dungeon_sbin, project_root / DUNGEON_INFO_PATH)
    dungeon_names = load_dungeon_names(project_root / DUNGEON_DATA_PATH)

    previews: List[Dict[str, object]] = []
    preview_dir = previews_root / "dungeon"
    for tileset in tilesets:
        tileset_id = int(tileset["tilesetId"])
        summary_path = preview_dir / f"tileset_{tileset_id:02d}.txt"
        summary_lines = [
            f"Tileset: {tileset_id}",
            f"Label: {tileset.get('label', '')}",
            f"Files: {', '.join(tileset.get('files', []))}",
        ]
        write_text(summary_path, "\n".join(summary_lines) + "\n")
        previews.append(
            {
                "kind": "dungeon_tileset",
                "tilesetId": tileset_id,
                "label": tileset.get("label", ""),
                "files": tileset.get("files", []),
                "summaryPath": str(summary_path.resolve()),
            }
        )

    floor_table = project_root / DUNGEON_FLOOR_ID_TABLE_PATH
    dungeon_folders = parse_dungeon_folder_order(floor_table)
    floor_mappings: List[Dict[str, object]] = []
    floor_index_lines: List[str] = []
    for dungeon_index, folder in enumerate(dungeon_folders):
        main_data_rel = DUNGEON_DIR / folder / "main_data.inc"
        main_data_path = project_root / main_data_rel
        if main_data_path.is_file():
            seed_editable_if_missing(project_root, editable_root, main_data_rel)
            editable_paths.append(main_data_rel)
        tilesets, parse_error = parse_floor_tilesets(main_data_path)
        if parse_error:
            errors.append(parse_error)

        dungeon_name = (
            dungeon_names[dungeon_index]
            if dungeon_index < len(dungeon_names)
            else f"DUNGEON_{folder.upper()}"
        )
        floors: List[Dict[str, object]] = []
        for floor_number, tileset_id in enumerate(tilesets, start=1):
            file_tileset_id = (
                remap[tileset_id]
                if 0 <= tileset_id < len(remap)
                else tileset_id
            )
            floors.append(
                {
                    "floorNumber": floor_number,
                    "tilesetId": tileset_id,
                    "fileTilesetId": file_tileset_id,
                }
            )
            floor_index_lines.append(
                f"{dungeon_name} F{floor_number:02d}: tileset={tileset_id:02d} fileSet=b{file_tileset_id:02d}"
            )

        floor_mappings.append(
            {
                "dungeonIndex": dungeon_index,
                "folder": folder,
                "dungeonName": dungeon_name,
                "floorCount": len(floors),
                "floors": floors,
            }
        )

    floor_summary_path = preview_dir / "floor_tilesets.txt"
    write_text(
        floor_summary_path,
        "\n".join(floor_index_lines) + ("\n" if floor_index_lines else ""),
    )
    previews.append(
        {
            "kind": "dungeon_floor_index",
            "summaryPath": str(floor_summary_path.resolve()),
            "dungeonCount": len(floor_mappings),
            "rowCount": len(floor_index_lines),
        }
    )

    asset_block = {
        "kind": "dungeon",
        "entries": tilesets,
        "tilesetFileRemap": remap,
        "dungeonNames": dungeon_names,
        "floorTilesets": floor_mappings,
    }
    return asset_block, previews, errors, sorted(set(editable_paths))


def load_manifest(index_path: Path) -> Dict[str, object] | None:
    if not index_path.is_file():
        return None
    try:
        return json.loads(read_text(index_path))
    except Exception:
        return None


def write_manifest(index_path: Path, manifest: Dict[str, object]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(manifest, indent=2, sort_keys=False)
    write_text(index_path, data + "\n")


def run_extract(project_root: Path, assets_root: Path, force: bool = False) -> int:
    editable_root, previews_root, _ = ensure_asset_directories(assets_root)
    index_path = assets_root / INDEX_NAME

    emit_progress("scan", 1, 7, "Computing source snapshot...")
    source_hashes = snapshot_hash(
        project_root,
        [
            POKEMON_SOURCE_ROOT,
            OVERWORLD_SOURCE_ROOT,
            DUNGEON_SBINS_PATH,
            MAP_FILES_TABLE_PATH,
            DUNGEON_INFO_PATH,
            DUNGEON_DATA_PATH,
        ],
    )

    existing = load_manifest(index_path)
    if (
        not force
        and existing
        and existing.get("version") == MANIFEST_VERSION
        and existing.get("sourceHashes") == source_hashes
    ):
        emit_progress("done", 7, 7, "Asset cache already up to date.")
        return 0

    emit_progress("pokemon", 2, 7, "Extracting Pokemon assets...")
    pokemon_assets, pokemon_previews, pokemon_errors, pokemon_editable = collect_pokemon(
        project_root, editable_root, previews_root
    )

    emit_progress("overworld", 3, 7, "Extracting overworld assets...")
    overworld_assets, overworld_previews, overworld_errors, overworld_editable = collect_overworld(
        project_root, editable_root, previews_root
    )

    emit_progress("dungeon", 4, 7, "Extracting dungeon assets...")
    dungeon_assets, dungeon_previews, dungeon_errors, dungeon_editable = collect_dungeon(
        project_root, editable_root, previews_root
    )

    editable_files = sorted(
        {
            str(path)
            for path in (
                pokemon_editable + overworld_editable + dungeon_editable
            )
        }
    )

    emit_progress("manifest", 5, 7, "Writing asset manifest...")
    manifest = {
        "version": MANIFEST_VERSION,
        "generatedAt": _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "projectRoot": str(project_root.resolve()),
        "sourceHashes": source_hashes,
        "assets": [pokemon_assets, dungeon_assets, overworld_assets],
        "repackRecipes": [
            {"kind": "mirror_copy", "sourceRoot": str((assets_root / EDITABLE_RELATIVE).resolve())},
            {"kind": "mirror_targets", "targets": ["graphics/ax/mon", "data/map_bg", "data/dungeon_sbin.s"]},
        ],
        "previewEntries": pokemon_previews + dungeon_previews + overworld_previews,
        "editableFiles": editable_files,
        "errors": pokemon_errors + dungeon_errors + overworld_errors,
    }
    write_manifest(index_path, manifest)

    emit_progress("finalize", 6, 7, "Finalizing extraction...")
    error_count = len(manifest["errors"])
    emit_progress(
        "done",
        7,
        7,
        f"Extraction complete. pokemon={len(pokemon_previews)} dungeon={len(dungeon_previews)} overworld={len(overworld_previews)} errors={error_count}",
    )
    return 0


def iter_editable_files(editable_root: Path) -> Iterable[Path]:
    if not editable_root.is_dir():
        return []
    return sorted([p for p in editable_root.rglob("*") if p.is_file()])


def tracked_editable_files(assets_root: Path, editable_root: Path) -> List[Path]:
    manifest = load_manifest(assets_root / INDEX_NAME)
    if not manifest:
        return list(iter_editable_files(editable_root))

    tracked = manifest.get("editableFiles")
    if not isinstance(tracked, list):
        return list(iter_editable_files(editable_root))

    files: List[Path] = []
    for value in tracked:
        if not isinstance(value, str) or not value:
            continue
        rel = Path(value)
        if rel.is_absolute() or ".." in rel.parts:
            continue
        candidate = editable_root / rel
        if candidate.is_file():
            files.append(candidate)
    return sorted(set(files))


def files_equivalent(path_a: Path, path_b: Path) -> bool:
    if not path_a.exists() or not path_b.exists():
        return False
    stat_a = path_a.stat()
    stat_b = path_b.stat()
    if stat_a.st_size != stat_b.st_size:
        return False
    if stat_a.st_mtime_ns == stat_b.st_mtime_ns:
        return True
    return file_sha256(path_a) == file_sha256(path_b)


def run_repack(project_root: Path, assets_root: Path) -> int:
    editable_root = assets_root / EDITABLE_RELATIVE
    if not editable_root.is_dir():
        emit_log("editable asset tree missing; run extract first")
        return 2

    files = tracked_editable_files(assets_root, editable_root)
    if not files:
        emit_progress("done", 0, 0, "Repack complete. changed=0 total=0")
        return 0

    total = len(files)
    changed = 0
    progress_interval = max(25, total // 40)

    emit_progress("repack_scan", 0, total, "Scanning editable assets for changes...")
    for idx, editable_file in enumerate(files, start=1):
        rel = editable_file.relative_to(editable_root)
        target = project_root / rel
        if not target.exists() or not files_equivalent(editable_file, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(editable_file, target)
            changed += 1
        if idx % progress_interval == 0 or idx == total:
            emit_progress("repack_copy", idx, total, f"Processed {idx}/{total} files...")

    emit_progress("done", total, total, f"Repack complete. changed={changed} total={total}")
    return 0


def run_extract_preview(project_root: Path, assets_root: Path, target: str) -> int:
    emit_log(f"Refreshing preview target '{target}' via incremental extract.")
    return run_extract(project_root, assets_root, force=True)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RedEditor PMD-Red asset pipeline helper.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="Extract/decompress assets into .rededitor/assets.")
    extract.add_argument("--project-root", required=True, help="Project root path (pmd-red clone).")
    extract.add_argument("--out", required=True, help="Output asset root directory.")
    extract.add_argument("--force", action="store_true", help="Force extraction even when hashes match.")

    repack = subparsers.add_parser("repack", help="Repack edited assets into project source files.")
    repack.add_argument("--project-root", required=True, help="Project root path (pmd-red clone).")
    repack.add_argument("--assets", required=True, help="Asset root directory (contains editable/).")

    preview = subparsers.add_parser(
        "extract-preview",
        help="Refresh preview artifacts for a target (pokemon, dungeon, overworld).",
    )
    preview.add_argument("--project-root", required=True, help="Project root path (pmd-red clone).")
    preview.add_argument("--assets", required=True, help="Asset root directory.")
    preview.add_argument("--target", required=True, choices=["pokemon", "dungeon", "overworld"])
    preview.add_argument("--id", required=False, help="Optional targeted ID (reserved).")

    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    if not project_root.is_dir():
        emit_log(f"project root not found: {project_root}")
        return 2

    if args.command == "extract":
        assets_root = Path(args.out).expanduser()
        if not assets_root.is_absolute():
            assets_root = project_root / assets_root
        return run_extract(project_root, assets_root, force=bool(args.force))

    if args.command == "repack":
        assets_root = Path(args.assets).expanduser()
        if not assets_root.is_absolute():
            assets_root = project_root / assets_root
        return run_repack(project_root, assets_root)

    if args.command == "extract-preview":
        assets_root = Path(args.assets).expanduser()
        if not assets_root.is_absolute():
            assets_root = project_root / assets_root
        return run_extract_preview(project_root, assets_root, args.target)

    emit_log(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
