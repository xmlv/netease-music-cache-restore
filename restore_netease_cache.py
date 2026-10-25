#!/usr/bin/env python3
"""Restore NetEase Music3 cache files using their cache index and song IDs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


XOR_KEY = 0xA3
XOR_TABLE = bytes.maketrans(
    bytes(range(256)), bytes(value ^ XOR_KEY for value in range(256))
)
CHUNK_SIZE = 1024 * 1024
MIN_CACHE_RATIO = 0.50
METADATA_BATCH_SIZE = 40
METADATA_RETRIES = 4
MAX_FILENAME_PART_LENGTH = 90
DEFAULT_SOURCE = Path.cwd() / "网易云音乐备份" / "Music3"
DEFAULT_OUTPUT = Path(r"D:\网易云音乐备份")


def identify_audio(header: bytes) -> tuple[str, str] | None:
    """Return (extension, format) for a recognized decoded audio header."""
    if len(header) >= 12 and header[4:8] == b"ftyp":
        if struct.unpack(">I", header[:4])[0] >= 8:
            return ".m4a", "M4A/MP4"
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
    ):
        return ".mp3", "MP3"
    if header.startswith(b"fLaC"):
        return ".flac", "FLAC"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return ".wav", "WAV"
    if header.startswith(b"OggS"):
        return ".ogg", "Ogg"
    return None


def song_id_from_file_id(file_id: str) -> str:
    parts = file_id.split("_")
    if len(parts) < 3 or not parts[0].isdigit():
        raise ValueError(f"unrecognized file_id: {file_id}")
    return parts[0]


def batches(values: list[str], size: int):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def request_song_batch(song_ids: list[str]) -> dict[str, dict[str, str]]:
    params = urllib.parse.urlencode(
        {
            "id": song_ids[0],
            "ids": json.dumps(song_ids, separators=(",", ":")),
        }
    )
    request = urllib.request.Request(
        f"https://music.163.com/api/song/detail/?{params}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.load(response)

    songs: dict[str, dict[str, str]] = {}
    for item in payload.get("songs", []):
        song_id = str(item.get("id", ""))
        artists = item.get("artists") or item.get("ar") or []
        artist_names = [
            str(artist.get("name", "")).strip()
            for artist in artists
            if str(artist.get("name", "")).strip()
        ]
        album_data = item.get("album") or item.get("al") or {}
        raw_title = item.get("name")
        title = str(raw_title).strip() if raw_title is not None else ""
        if song_id and title:
            songs[song_id] = {
                "title": title,
                "artist": ", ".join(artist_names) or "未知歌手",
                "album": str(album_data.get("name", "")).strip(),
            }
    return songs


def fetch_song_metadata(song_ids: list[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Fetch metadata in bounded batches, retry missing IDs, and return unresolved IDs."""
    metadata: dict[str, dict[str, str]] = {}
    failed_batches: list[list[str]] = []
    song_batches = list(batches(song_ids, METADATA_BATCH_SIZE))
    for number, batch in enumerate(song_batches, start=1):
        for attempt in range(METADATA_RETRIES):
            try:
                metadata.update(request_song_batch(batch))
                break
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
                if attempt + 1 == METADATA_RETRIES:
                    failed_batches.append(batch)
                    print(
                        f"WARNING: metadata batch {number} failed after {METADATA_RETRIES} attempts: {error}",
                        file=sys.stderr,
                    )
                else:
                    time.sleep(1.0 * (attempt + 1))
        time.sleep(0.2)
        print(
            f"Catalog lookup {number}/{len(song_batches)}: "
            f"matched {len(metadata)} of {len(song_ids)} IDs",
            flush=True,
        )

    unresolved = {song_id for batch in failed_batches for song_id in batch}
    unresolved.update(set(song_ids) - set(metadata))
    # Avoid turning a catalogue outage into hundreds of serial requests.
    if not metadata:
        return metadata, sorted(unresolved)
    # Individual fallback handles a bounded number of transient/missing entries.
    # Remaining IDs use deterministic ID-based names and are listed in the report.
    for song_id in sorted(unresolved)[:40]:
        for attempt in range(2):
            try:
                metadata.update(request_song_batch([song_id]))
                if song_id in metadata:
                    break
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
                if attempt + 1 == 2:
                    print(f"WARNING: song ID {song_id} lookup failed: {error}", file=sys.stderr)
            time.sleep(0.5 * (attempt + 1))
    return metadata, sorted(set(song_ids) - set(metadata))


def safe_filename_part(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    cleaned = cleaned.strip().rstrip(". ")[:MAX_FILENAME_PART_LENGTH].rstrip(". ")
    return cleaned or "未知"


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # A cleanup failure must be logged by its caller, never crash the whole batch.
        pass


def infer_mp4_expected_size(source: Path) -> int | None:
    """Infer total MP4 size from top-level boxes when an .nmsfi sidecar is absent."""
    actual_size = source.stat().st_size
    offset = 0
    last_end = 0
    with source.open("rb") as stream:
        while offset + 8 <= actual_size:
            stream.seek(offset)
            raw = stream.read(16).translate(XOR_TABLE)
            if len(raw) < 8:
                return None
            box_size = struct.unpack(">I", raw[:4])[0]
            box_type = raw[4:8]
            header_size = 8
            if box_size == 1:
                if len(raw) < 16:
                    return None
                box_size = struct.unpack(">Q", raw[8:16])[0]
                header_size = 16
            elif box_size == 0:
                # ISO BMFF size 0 explicitly means the box extends to current EOF.
                return actual_size
            if box_size < header_size:
                return None
            end = offset + box_size
            last_end = end
            if box_type == b"mdat" and end > actual_size:
                return end
            if end > actual_size:
                return end
            offset = end
    if offset == actual_size:
        return actual_size
    return last_end if last_end >= actual_size else None


def cache_ratio(source: Path, index_path: Path | None) -> tuple[float | None, int, int | None, str]:
    """Estimate present bytes from trunks, falling back to MP4 box lengths."""
    actual = source.stat().st_size
    if index_path is not None and index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        expected = int(index.get("file_size") or 0)
        if expected <= 0:
            raise ValueError(f"missing/invalid file_size in {index_path.name}")
        trunk_bytes = 0
        has_trunks = False
        for part in index.get("slices", []):
            for trunk in part.get("trunks", []):
                has_trunks = True
                trunk_bytes += max(0, int(trunk.get("size", 0)))
        observed = trunk_bytes if has_trunks else actual
        cached = min(observed, actual, expected)
        return cached / expected, cached, expected, "nmsfi"

    expected = infer_mp4_expected_size(source)
    if expected and expected > 0:
        cached = min(actual, expected)
        return cached / expected, cached, expected, "mp4-boxes"
    return None, actual, None, "unknown-no-index"


def decode_to_temp(source: Path, temporary: Path, expected_md5: str | None) -> tuple[int, str]:
    digest = hashlib.md5()
    total = 0
    with source.open("rb") as src, temporary.open("wb") as dst:
        while chunk := src.read(CHUNK_SIZE):
            decoded = chunk.translate(XOR_TABLE)
            dst.write(decoded)
            digest.update(decoded)
            total += len(decoded)
    shutil.copystat(source, temporary)
    actual_md5 = digest.hexdigest()
    if expected_md5 and actual_md5 != expected_md5.lower():
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"decoded MD5 mismatch: expected {expected_md5}, got {actual_md5}"
        )
    return total, actual_md5


def remove_legacy_output(output_dir: Path, file_id: str) -> None:
    """Remove only previous ID-named derived outputs, never source backups."""
    for suffix in (".m4a", ".mp3", ".flac", ".wav", ".ogg"):
        safe_unlink(output_dir / f"{file_id}_0{suffix}")
    safe_unlink(output_dir / f"{file_id}.config")
    safe_unlink(output_dir / f"{file_id}.nmsfi")


def remove_stale_named_outputs(output_dir: Path, base: str, keep: set[Path]) -> None:
    """Remove prior script outputs for this exact title group if their names changed."""
    if not output_dir.is_dir():
        return
    allowed_suffixes = {".m4a", ".mp3", ".flac", ".wav", ".ogg", ".config"}
    for path in output_dir.iterdir():
        if not path.is_file() or path in keep or path.suffix.lower() not in allowed_suffixes:
            continue
        if path.stem == base or path.stem.startswith(base + " ["):
            safe_unlink(path)


def clean_orphaned_cache_sidecars(output_dir: Path) -> list[Path]:
    """Remove old cache-only metadata copied by the previous restore workflow."""
    removed: list[Path] = []
    if not output_dir.is_dir():
        return removed
    for path in output_dir.glob("*.nmsfi"):
        has_id_audio = any(
            output_dir.joinpath(f"{path.stem}_0.{extension}").is_file()
            for extension in ("m4a", "mp3", "flac", "wav", "ogg")
        )
        if not has_id_audio:
            safe_unlink(path)
            removed.append(path)
    for path in output_dir.glob("*.config"):
        has_id_audio = any(
            output_dir.joinpath(f"{path.stem}_0.{extension}").is_file()
            for extension in ("m4a", "mp3", "flac", "wav", "ogg")
        )
        if re.fullmatch(r"\d+_\d+_[0-9a-fA-F]{32}", path.stem) and not has_id_audio:
            safe_unlink(path)
            removed.append(path)
    return removed


def config_format(config_path: Path | None, audio_format: str) -> str:
    if config_path and config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            direct = config.get("audioFormat")
            if direct:
                return str(direct)
            for value in config.values():
                if isinstance(value, dict) and value.get("audioFormat"):
                    return str(value["audioFormat"])
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "M4A/MP4": "m4a",
        "MP3": "mp3",
        "FLAC": "flac",
        "WAV": "wav",
        "Ogg": "ogg",
    }.get(audio_format, audio_format.lower())


def write_run_report(output_dir: Path, rows: list[dict[str, str]]) -> None:
    report = output_dir / "restore_report.csv"
    fields = [
        "source",
        "file_id",
        "song_id",
        "title",
        "artist",
        "cache_percent",
        "status",
        "output_audio",
        "output_config",
        "details",
    ]
    with report.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

    parser = argparse.ArgumentParser(
        description="Recursively restore .nmsf songs, name them, preserve config, and filter low cache."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--limit", type=int, help="process only the first N .nmsf files sorted by path"
    )
    overwrite_group = parser.add_mutually_exclusive_group()
    overwrite_group.add_argument(
        "--overwrite", dest="overwrite", action="store_true", help="replace named outputs (default)"
    )
    overwrite_group.add_argument(
        "--no-overwrite", dest="overwrite", action="store_false", help="stop if a named output already exists"
    )
    parser.set_defaults(overwrite=True)
    parser.add_argument(
        "--dry-run", action="store_true", help="check indexes and metadata without writing files"
    )
    parser.add_argument(
        "--cleanup-only", action="store_true", help="remove obsolete cache-only sidecars from output"
    )
    args = parser.parse_args()

    source_root = args.source.resolve()
    output_dir = args.output.resolve()
    if args.cleanup_only:
        removed = clean_orphaned_cache_sidecars(output_dir)
        print(f"Removed {len(removed)} obsolete cache-only sidecar(s) from {output_dir}")
        return 0
    if not source_root.is_dir():
        parser.error(f"source directory does not exist: {source_root}")
    if source_root == output_dir or source_root in output_dir.parents:
        parser.error("output directory must be outside the source directory")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")

    source_files = sorted(source_root.rglob("*.nmsf"), key=lambda item: str(item.relative_to(source_root)).casefold())
    if args.limit is not None:
        source_files = source_files[: args.limit]
    if not source_files:
        parser.error(f"no .nmsf files found below: {source_root}")

    records: list[dict[str, Any]] = []
    report_rows: list[dict[str, str]] = []
    audio_file_ids = {
        path.name.removesuffix("_0.nmsf")
        for path in source_root.rglob("*.nmsf")
        if path.name.endswith("_0.nmsf")
    }
    orphan_sidecar_ids: dict[str, list[Path]] = defaultdict(list)
    for pattern in ("*.nmsfi", "*.config"):
        for sidecar in source_root.rglob(pattern):
            if sidecar.stem not in audio_file_ids:
                orphan_sidecar_ids[sidecar.stem].append(sidecar)
    for file_id, paths in orphan_sidecar_ids.items():
        report_rows.append(
            {
                "source": "; ".join(str(path) for path in paths),
                "file_id": file_id,
                "song_id": file_id.split("_")[0] if "_" in file_id else "",
                "status": "sidecar-without-audio",
                "details": "no corresponding .nmsf audio file exists in the source backup",
            }
        )
    for source in source_files:
        file_id = source.name.removesuffix("_0.nmsf")
        index_path = source.with_name(f"{file_id}.nmsfi")
        config_path = source.with_name(f"{file_id}.config")
        try:
            song_id = song_id_from_file_id(file_id)
            ratio, cached, expected, ratio_source = cache_ratio(source, index_path)
            with source.open("rb") as stream:
                audio_type = identify_audio(stream.read(32).translate(XOR_TABLE))
            if audio_type is None:
                raise ValueError("unrecognized decoded audio header")
            records.append(
                {
                    "source": source,
                    "file_id": file_id,
                    "song_id": song_id,
                    "index": index_path if index_path.is_file() else None,
                    "config": config_path if config_path.is_file() else None,
                    "ratio": ratio,
                    "cached": cached,
                    "expected": expected,
                    "ratio_source": ratio_source,
                    "audio_type": audio_type,
                    "md5": file_id.split("_")[-1],
                    "variant": file_id.split("_")[1],
                }
            )
        except (OSError, ValueError, json.JSONDecodeError, IndexError) as error:
            print(f"ERROR: {source}: {error}", file=sys.stderr)
            report_rows.append(
                {
                    "source": str(source),
                    "file_id": file_id,
                    "song_id": file_id.split("_")[0] if "_" in file_id else "",
                    "status": "error-preflight",
                    "details": str(error),
                }
            )

    # If duplicate file IDs exist, keep the copy with the greatest known cache coverage.
    unique_records: dict[str, dict[str, Any]] = {}
    for record in records:
        previous = unique_records.get(record["file_id"])
        current_ratio = -1.0 if record["ratio"] is None else record["ratio"]
        previous_ratio = (
            -1.0
            if previous is None or previous["ratio"] is None
            else previous["ratio"]
        )
        if previous is None or current_ratio > previous_ratio:
            unique_records[record["file_id"]] = record
    records = list(unique_records.values())

    try:
        metadata, missing_ids = fetch_song_metadata(
            sorted({str(record["song_id"]) for record in records})
        )
    except Exception as error:
        # Do not depend on an LLM or abort the batch when the catalogue is unavailable.
        print(f"WARNING: metadata service unavailable; using ID-based names: {error}", file=sys.stderr)
        metadata, missing_ids = {}, sorted({str(record["song_id"]) for record in records})

    name_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        song = metadata.get(str(record["song_id"]))
        if song is None:
            song = {
                "title": f"歌曲ID {record['song_id']}",
                "artist": "元数据未找到",
                "album": "",
            }
            record["metadata_fallback"] = True
        else:
            record["metadata_fallback"] = False
        record["song"] = song
        base = f"{safe_filename_part(song['title'])} - {safe_filename_part(song['artist'])}"
        record["base"] = base
        name_groups[base.casefold()].append(record)

    for group in name_groups.values():
        variant_counts = Counter(record["variant"] for record in group)
        for record in group:
            variant = str(record["variant"])
            if len(group) == 1:
                suffix = ""
            elif variant_counts[variant] == 1:
                suffix = f" [{variant}]"
            else:
                suffix = f" [{variant}-{str(record['md5'])[:8]}]"
            extension = record["audio_type"][0]
            record["destination"] = output_dir / f"{record['base']}{suffix}{extension}"
            record["config_destination"] = output_dir / f"{record['base']}{suffix}.config"

    # Guarantee unique audio/config stems even if different catalogue IDs share
    # the same title, artist, and quality token.
    used_stems: set[str] = set()
    for record in records:
        stem = record["destination"].stem
        key = stem.casefold()
        if key in used_stems:
            identity = (
                f" [{record['song_id']}-{record['variant']}-{str(record['md5'])[:8]}]"
            )
            candidate = f"{record['base']}{identity}"
            suffix_number = 2
            while candidate.casefold() in used_stems:
                candidate = f"{record['base']}{identity}-{suffix_number}"
                suffix_number += 1
            record["destination"] = output_dir / f"{candidate}{record['audio_type'][0]}"
            record["config_destination"] = output_dir / f"{candidate}.config"
            key = candidate.casefold()
        used_stems.add(key)

    if args.dry_run:
        low_count = sum(
            record["ratio"] is not None and record["ratio"] < MIN_CACHE_RATIO
            for record in records
        )
        partial_count = sum(
            record["ratio"] is not None and MIN_CACHE_RATIO <= record["ratio"] < 1.0
            for record in records
        )
        full_count = sum(
            record["ratio"] is not None and record["ratio"] >= 1.0
            for record in records
        )
        print(f"Dry run: discovered {len(source_files)} .nmsf file(s) in {source_root}")
        print(f"Preflight-valid unique cache IDs: {len(records)}")
        print(
            "Preflight errors: "
            + str(sum(row.get("status") == "error-preflight" for row in report_rows))
        )
        print(f"Sidecar-only entries with no audio source: {len(orphan_sidecar_ids)}")
        print(f"Coverage: full={full_count}, partial 50%-99%={partial_count}, below 50%={low_count}")
        print(f"Missing .nmsfi inferred from MP4 boxes: {sum(record['ratio_source'] == 'mp4-boxes' for record in records)}")
        print(f"Missing .config to synthesize if retained: {sum(record['config'] is None for record in records)}")
        print(f"Song IDs using fallback names: {len(missing_ids)}")
        for song_id in missing_ids:
            print(f"  {song_id}")
        if report_rows:
            print("Preflight errors:")
            for row in report_rows:
                print(f"  {row['source']}: {row['details']}")
        return 1 if any(row.get("status") == "error-preflight" for row in report_rows) else 0

    collisions: list[Path] = []
    for record in records:
        if record["ratio"] is not None and record["ratio"] < MIN_CACHE_RATIO:
            continue
        for destination in (record["destination"], record["config_destination"]):
            if destination.exists() and not args.overwrite:
                collisions.append(destination)
    if collisions:
        print(
            f"ERROR: {len(collisions)} named output file(s) already exist; "
            f"remove --no-overwrite to replace them. Nothing was changed. Example: {collisions[0]}",
            file=sys.stderr,
        )
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    formats: Counter[str] = Counter()
    restored = 0
    removed_low: list[str] = []
    partial_kept: list[tuple[str, float]] = []
    unknown_coverage: list[str] = []
    metadata_fallbacks: list[str] = []
    errors: list[str] = [
        f"{row['source']}: {row['details']}"
        for row in report_rows
        if row.get("status") == "error-preflight"
    ]

    for record_number, record in enumerate(records, start=1):
        song = record["song"]
        display_name = f"{song['title']} - {song['artist']}"
        ratio = record["ratio"]
        source = record["source"]
        file_id = str(record["file_id"])
        destination = record["destination"]
        config_destination = record["config_destination"]
        row: dict[str, str] = {
            "source": str(source),
            "file_id": file_id,
            "song_id": str(record["song_id"]),
            "title": str(song["title"]),
            "artist": str(song["artist"]),
            "cache_percent": "unknown" if ratio is None else f"{ratio:.1%}",
            "output_audio": str(destination),
            "output_config": str(config_destination),
        }

        if record["metadata_fallback"]:
            metadata_fallbacks.append(f"{display_name} ({record['song_id']})")

        if record_number % 100 == 0:
            print(f"Processing cache {record_number}/{len(records)}", flush=True)

        if ratio is None:
            unknown_coverage.append(f"{display_name} ({file_id})")
            row.update(status="skipped-unknown-coverage", details="missing index and no inferable container size")
            record["run_status"] = "skipped-unknown-coverage"
            report_rows.append(row)
            continue

        if ratio < MIN_CACHE_RATIO:
            remove_legacy_output(output_dir, file_id)
            safe_unlink(destination)
            safe_unlink(config_destination)
            removed_low.append(f"{display_name} ({ratio:.1%}, {file_id})")
            row["status"] = "removed-below-50-percent"
            record["run_status"] = "removed-below-50-percent"
            report_rows.append(row)
            continue

        temporary = destination.with_name(destination.name + ".restore-tmp")
        try:
            expected_md5 = str(record["md5"]) if ratio >= 1.0 else None
            decoded_size, decoded_md5 = decode_to_temp(source, temporary, expected_md5)
            os.replace(temporary, destination)
            if record["config"] is not None:
                shutil.copy2(record["config"], config_destination)
            else:
                generated = {
                    "audioFormat": config_format(None, record["audio_type"][1])
                }
                config_destination.write_text(
                    json.dumps(generated, ensure_ascii=False, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
            remove_legacy_output(output_dir, file_id)
            restored += 1
            formats[record["audio_type"][1]] += 1
            row["status"] = "restored"
            record["run_status"] = "restored"
            row["details"] = (
                f"decoded_bytes={decoded_size}; md5={decoded_md5}; "
                f"coverage_source={record['ratio_source']}"
            )
            if ratio < 1.0:
                partial_kept.append((display_name, ratio))
        except (OSError, ValueError) as error:
            safe_unlink(temporary)
            errors.append(f"{display_name}: {error}")
            row["status"] = "error-restore"
            record["run_status"] = "error-restore"
            row["details"] = str(error)
        report_rows.append(row)

    # Only clean old title-based names after every item for that title group
    # has reached a final restore/remove state. Failed groups keep their old files.
    for group in name_groups.values():
        if all(record.get("run_status") in {"restored", "removed-below-50-percent"} for record in group):
            keep = {
                record[key]
                for record in group
                for key in ("destination", "config_destination")
            }
            remove_stale_named_outputs(output_dir, group[0]["base"], keep)

    removed_sidecars = clean_orphaned_cache_sidecars(output_dir)
    write_run_report(output_dir, report_rows)
    print(f"Processed {len(source_files)} cache file(s) below {source_root}")
    print(f"Unique cache IDs: {len(records)}; restored and named: {restored}")
    for audio_format, count in sorted(formats.items()):
        print(f"  {audio_format}: {count}")
    print(f"Low-cache songs removed from output (<50%): {len(removed_low)}")
    for name in removed_low:
        print(f"  {name}")
    print(f"Partial songs retained (50%-99%): {len(partial_kept)}")
    for name, ratio in partial_kept:
        print(f"  {name} ({ratio:.1%})")
    if metadata_fallbacks:
        fallback_ids = sorted(
            {str(record["song_id"]) for record in records if record["metadata_fallback"]}
        )
        print(
            f"Names using ID fallback: {len(fallback_ids)} song ID(s), "
            f"{len(metadata_fallbacks)} file(s)"
        )
        for name in metadata_fallbacks:
            print(f"  {name}")
    if unknown_coverage:
        print(f"Skipped because coverage could not be inferred: {len(unknown_coverage)}")
        for name in unknown_coverage:
            print(f"  {name}")
    if errors:
        print(f"Restore errors: {len(errors)}")
        for error in errors:
            print(f"  {error}")
    print(f"Obsolete cache-only sidecars removed: {len(removed_sidecars)}")
    print(f"Source sidecar groups without audio: {len(orphan_sidecar_ids)}")
    print(f"Run report: {output_dir / 'restore_report.csv'}")
    print(f"Output directory: {output_dir}")
    return 1 if errors or unknown_coverage else 0


if __name__ == "__main__":
    raise SystemExit(main())
