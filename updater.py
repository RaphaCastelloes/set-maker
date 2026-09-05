#!/usr/bin/env python3

import argparse
import json
import math
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path


ARTIST_PREFIX = re.compile(r"^\s*\d{1,2}[AB]\*?(?:\|\d{1,2})?\s+", re.IGNORECASE)


def _normalize_path(value):
    path = "/" + str(value).replace("\\", "/").lstrip("/")
    return unicodedata.normalize("NFC", path).casefold()


def _load_analysis(path):
    analysis_path = Path(path).expanduser()
    if not analysis_path.is_file():
        raise FileNotFoundError(f"Analysis JSON not found: {analysis_path}")
    with analysis_path.open(encoding="utf-8") as file:
        data = json.load(file)
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("The JSON must contain a folder analysis with a tracks list")
    return data, tracks


def _database_path(data, override):
    value = override or data.get("rekordbox", {}).get("database")
    if not value:
        raise ValueError(
            "No Rekordbox database in the analysis; provide --database exportLibrary.db"
        )
    path = Path(value).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Rekordbox database not found: {path}")
    if path.name != "exportLibrary.db":
        raise ValueError("This updater supports only Device Library Plus exportLibrary.db")
    return path


def _usb_root(database_path):
    try:
        root = database_path.resolve().parents[2]
    except IndexError as exc:
        raise ValueError("Could not determine the USB root from the database path") from exc
    expected = root / "PIONEER" / "rekordbox" / "exportLibrary.db"
    if expected.resolve() != database_path.resolve():
        raise ValueError(
            "Database must be located at USB_ROOT/PIONEER/rekordbox/exportLibrary.db"
        )
    return root


def _track_values(track):
    try:
        bpm = float(track["tempo"]["bpm"])
        camelot = str(track["key"]["camelot"]).strip().upper()
        relative = int(track["collection"]["dj_level"])
        audio_file = Path(track["file"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("missing BPM, Camelot key, relative level, or file path") from exc
    if not math.isfinite(bpm) or bpm <= 0:
        raise ValueError("invalid BPM")
    if not re.fullmatch(r"(?:[1-9]|1[0-2])[AB]", camelot):
        raise ValueError("invalid Camelot key")
    if not 1 <= relative <= 10:
        raise ValueError("relative level must be between 1 and 10")
    bpm_integer = int(math.floor(bpm + 0.5))
    return audio_file, bpm_integer, camelot, relative


def _database_track_path(audio_file, usb_root):
    try:
        relative = audio_file.resolve().relative_to(usb_root.resolve())
    except ValueError:
        return None
    return _normalize_path(relative)


def _artist_name(content):
    if content.artist and content.artist.name.strip():
        return content.artist.name.strip(), "rekordbox"
    title = str(content.title or "").strip()
    if " - " not in title:
        title = Path(str(content.fileName or content.path)).stem
    artist, separator, _ = title.partition(" - ")
    return (artist.strip(), "filename") if separator and artist.strip() else ("", None)


def build_plan(analysis_tracks, database_path):
    try:
        from pyrekordbox import DeviceLibraryPlus
    except ImportError as exc:
        raise RuntimeError(
            "Pyrekordbox is missing. Run with the project virtual environment."
        ) from exc

    root = _usb_root(database_path)
    changes = []
    unchanged = []
    unmatched = []
    invalid = []
    with DeviceLibraryPlus(database_path) as database:
        contents = {
            _normalize_path(content.path): content for content in database.get_content()
        }
        for track in analysis_tracks:
            display_path = str(track.get("file", "<unknown>"))
            try:
                audio_file, bpm, camelot, relative = _track_values(track)
            except ValueError as exc:
                invalid.append({"file": display_path, "reason": str(exc)})
                continue
            lookup_path = _database_track_path(audio_file, root)
            content = contents.get(lookup_path) if lookup_path else None
            if content is None:
                unmatched.append(display_path)
                continue
            old_artist = content.artist.name.strip() if content.artist else ""
            artist_name, artist_source = _artist_name(content)
            base_artist = ARTIST_PREFIX.sub("", artist_name).strip()
            if not base_artist:
                invalid.append(
                    {"file": display_path, "reason": "artist missing and not found in filename"}
                )
                continue
            old_album = content.album.name.strip() if content.album else ""
            new_artist = f"{camelot}|{relative} {base_artist}"
            new_album = f"{bpm:03d}|{camelot}|{relative}"
            change = {
                "content_id": content.content_id,
                "path": content.path,
                "old_artist": old_artist,
                "new_artist": new_artist,
                "artist_source": artist_source,
                "old_album": old_album,
                "new_album": new_album,
            }
            if old_artist == new_artist and old_album == new_album:
                unchanged.append(change)
            else:
                changes.append(change)
    return {
        "database": str(database_path.resolve()),
        "usb_root": str(root),
        "changes": changes,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "invalid": invalid,
    }


def _get_or_create_artist(database, name):
    artist = database.get_artist(name=name).one_or_none()
    return artist or database.add_artist(name, search_string=name)


def _get_or_create_album(database, name):
    album = database.get_album(name=name).one_or_none()
    return album or database.add_album(name, search_string=name)


def apply_plan(plan):
    from pyrekordbox import DeviceLibraryPlus

    with DeviceLibraryPlus(plan["database"]) as database:
        try:
            for change in plan["changes"]:
                content = database.get_content(id=change["content_id"])
                if content is None:
                    raise RuntimeError(
                        f"Track disappeared from database: {change['path']}"
                    )
                artist = _get_or_create_artist(database, change["new_artist"])
                album = _get_or_create_album(database, change["new_album"])
                content.artist_id_artist = artist.artist_id
                content.album_id = album.album_id
            database.commit()
        except Exception:
            database.rollback()
            raise


def create_backup(database_path, backup_directory=None):
    directory = Path(backup_directory).expanduser() if backup_directory else database_path.parent
    if not directory.is_dir():
        raise NotADirectoryError(f"Backup directory not found: {directory}")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = directory / f"{database_path.name}.backup-{timestamp}"
    shutil.copy2(database_path, destination)
    return destination


def print_plan(plan, applying=False):
    mode = "APPLY" if applying else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Database: {plan['database']}")
    print(f"Planned changes: {len(plan['changes'])}")
    print(f"Unchanged: {len(plan['unchanged'])}")
    print(f"Not in Rekordbox database: {len(plan['unmatched'])}")
    print(f"Invalid: {len(plan['invalid'])}")
    for change in plan["changes"]:
        print(f"\n{change['path']}")
        old_artist = change["old_artist"] or "<missing>"
        source = " (inferred from filename)" if change["artist_source"] == "filename" else ""
        print(f"  Artist: {old_artist} -> {change['new_artist']}{source}")
        print(f"  Album:  {change['old_album']} -> {change['new_album']}")
    if plan["unmatched"]:
        print("\nNot in Rekordbox database:")
        for path in plan["unmatched"]:
            print(f"  {path}")
    if plan["invalid"]:
        print("\nInvalid analysis entries:", file=sys.stderr)
        for item in plan["invalid"]:
            print(f"  {item['file']}: {item['reason']}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Update Rekordbox USB artist and album conventions from analyzer JSON."
    )
    parser.add_argument("analysis_json", help="JSON produced by analizer.py --json")
    parser.add_argument(
        "--database",
        help="Path to exportLibrary.db; defaults to the path stored in the JSON",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a backup and commit changes; otherwise perform a dry run",
    )
    parser.add_argument(
        "--backup-directory",
        help="Directory for the database backup; defaults to the database directory",
    )
    args = parser.parse_args()

    try:
        data, tracks = _load_analysis(args.analysis_json)
        database_path = _database_path(data, args.database)
        plan = build_plan(tracks, database_path)
        print_plan(plan, applying=args.apply)
        if not args.apply:
            print("\nNo changes written. Review the plan, then rerun with --apply.")
            return 0
        if not plan["changes"]:
            print("\nNo changes are required.")
            return 0
        backup = create_backup(database_path, args.backup_directory)
        print(f"\nBackup created: {backup}")
        apply_plan(plan)
        print(f"Updated {len(plan['changes'])} track(s).")
        return 0
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
