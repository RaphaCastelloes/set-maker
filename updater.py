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
DATABASE_NAMES = ("exportLibrary.db", "export.pdb")


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


def _usb_root(database_path):
    try:
        root = database_path.resolve().parents[2]
    except IndexError as exc:
        raise ValueError("Could not determine the USB root from the database path") from exc
    expected_directory = root / "PIONEER" / "rekordbox"
    if database_path.resolve().parent != expected_directory.resolve():
        raise ValueError(
            "Database must be located at USB_ROOT/PIONEER/rekordbox/"
        )
    return root


def _database_paths(data, override):
    value = override or data.get("rekordbox", {}).get("database")
    if not value:
        raise ValueError(
            "No Rekordbox database in the analysis; provide --database exportLibrary.db"
        )
    supplied = Path(value).expanduser()
    if not supplied.is_file():
        raise FileNotFoundError(f"Rekordbox database not found: {supplied}")
    if supplied.name not in DATABASE_NAMES:
        raise ValueError("Database must be exportLibrary.db or export.pdb")
    root = _usb_root(supplied)
    directory = root / "PIONEER" / "rekordbox"
    paths = [directory / name for name in DATABASE_NAMES]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Both Rekordbox device databases are required; missing: " + ", ".join(missing)
        )
    return paths


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


def _artist_name(artist, title, filename, path):
    if artist and artist.strip():
        return artist.strip(), "rekordbox"
    title = str(title or "").strip()
    if " - " not in title:
        title = Path(str(filename or path)).stem
    artist, separator, _ = title.partition(" - ")
    return (artist.strip(), "filename") if separator and artist.strip() else ("", None)


def _new_values(track):
    audio_file, bpm, camelot, relative = _track_values(track)
    return audio_file, f"{camelot}|{relative}", f"{bpm:03d}|{camelot}|{relative}"


def _finish_plan(database_path, root, changes, unchanged, unmatched, invalid, kind):
    return {
        "kind": kind,
        "database": str(database_path.resolve()),
        "usb_root": str(root),
        "changes": changes,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "invalid": invalid,
    }


def build_plus_plan(analysis_tracks, database_path):
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
                audio_file, artist_prefix, new_album = _new_values(track)
            except ValueError as exc:
                invalid.append({"file": display_path, "reason": str(exc)})
                continue
            lookup_path = _database_track_path(audio_file, root)
            content = contents.get(lookup_path) if lookup_path else None
            if content is None:
                unmatched.append(display_path)
                continue
            old_artist = content.artist.name.strip() if content.artist else ""
            artist_name, artist_source = _artist_name(
                old_artist, content.title, content.fileName, content.path
            )
            base_artist = ARTIST_PREFIX.sub("", artist_name).strip()
            if not base_artist:
                invalid.append(
                    {"file": display_path, "reason": "artist missing and not found in filename"}
                )
                continue
            old_album = content.album.name.strip() if content.album else ""
            new_artist = f"{artist_prefix} {base_artist}"
            change = {
                "content_id": content.content_id,
                "path": content.path,
                "old_artist": old_artist,
                "new_artist": new_artist,
                "artist_source": artist_source,
                "old_album": old_album,
                "new_album": new_album,
            }
            (unchanged if old_artist == new_artist and old_album == new_album else changes).append(change)
    return _finish_plan(
        database_path, root, changes, unchanged, unmatched, invalid, "Device Library Plus"
    )


def build_classic_plan(analysis_tracks, database_path):
    try:
        from rekordbox_pdb import Database
    except ImportError as exc:
        raise RuntimeError(
            "rekordbox-pdb is missing. Install the pinned dependency from README.md."
        ) from exc

    root = _usb_root(database_path)
    database = Database.from_file(database_path)
    artists = {artist.id: artist.name for artist in database.artists}
    albums = {album.id: album.name for album in database.albums}
    contents = {_normalize_path(content.file_path): content for content in database.tracks}
    changes = []
    unchanged = []
    unmatched = []
    invalid = []
    for track in analysis_tracks:
        display_path = str(track.get("file", "<unknown>"))
        try:
            audio_file, artist_prefix, new_album = _new_values(track)
        except ValueError as exc:
            invalid.append({"file": display_path, "reason": str(exc)})
            continue
        lookup_path = _database_track_path(audio_file, root)
        content = contents.get(lookup_path) if lookup_path else None
        if content is None:
            unmatched.append(display_path)
            continue
        old_artist = str(artists.get(content.artist_id, "")).strip()
        artist_name, artist_source = _artist_name(
            old_artist, content.title, content.filename, content.file_path
        )
        base_artist = ARTIST_PREFIX.sub("", artist_name).strip()
        if not base_artist:
            invalid.append(
                {"file": display_path, "reason": "artist missing and not found in filename"}
            )
            continue
        old_album = str(albums.get(content.album_id, "")).strip()
        new_artist = f"{artist_prefix} {base_artist}"
        change = {
            "track_id": content.id,
            "path": content.file_path,
            "old_artist": old_artist,
            "new_artist": new_artist,
            "artist_source": artist_source,
            "old_album": old_album,
            "new_album": new_album,
        }
        (unchanged if old_artist == new_artist and old_album == new_album else changes).append(change)
    return _finish_plan(
        database_path, root, changes, unchanged, unmatched, invalid, "Device Library"
    )


def build_plans(analysis_tracks, database_paths):
    return [
        build_plus_plan(analysis_tracks, database_paths[0]),
        build_classic_plan(analysis_tracks, database_paths[1]),
    ]


def _get_or_create_artist(database, name):
    artist = database.get_artist(name=name).one_or_none()
    return artist or database.add_artist(name, search_string=name)


def _get_or_create_album(database, name):
    album = database.get_album(name=name).one_or_none()
    return album or database.add_album(name, search_string=name)


def apply_plus_plan(plan):
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


def apply_classic_plan(plan):
    from rekordbox_pdb.edit import PdbEditor

    database = PdbEditor.from_file(plan["database"])
    for change in plan["changes"]:
        artist_id = database.get_or_create_artist(change["new_artist"])
        album_id = database.get_or_create_album(change["new_album"])
        database.set_track_field(change["track_id"], "artist_id", artist_id)
        database.set_track_field(change["track_id"], "album_id", album_id)
    database.save(plan["database"])


def create_backup(database_path, backup_directory=None):
    directory = Path(backup_directory).expanduser() if backup_directory else database_path.parent
    if not directory.is_dir():
        raise NotADirectoryError(f"Backup directory not found: {directory}")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = directory / f"{database_path.name}.backup-{timestamp}"
    shutil.copy2(database_path, destination)
    return destination


def apply_plans(plans, backup_directory=None):
    backups = {
        plan["database"]: create_backup(Path(plan["database"]), backup_directory)
        for plan in plans
    }
    try:
        apply_plus_plan(plans[0])
        apply_classic_plan(plans[1])
    except Exception:
        for database, backup in backups.items():
            shutil.copy2(backup, database)
        raise
    return backups


def print_plan(plan, applying=False):
    mode = "APPLY" if applying else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Library: {plan['kind']}")
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
        description="Update both Rekordbox USB artist and album databases from analyzer JSON."
    )
    parser.add_argument("analysis_json", help="JSON produced by analizer.py --json")
    parser.add_argument(
        "--database",
        help="Path to exportLibrary.db or export.pdb; defaults to the path stored in the JSON",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up and update both databases; otherwise perform a dry run",
    )
    parser.add_argument(
        "--backup-directory",
        help="Directory for both database backups; defaults to the database directory",
    )
    args = parser.parse_args()

    try:
        data, tracks = _load_analysis(args.analysis_json)
        database_paths = _database_paths(data, args.database)
        plans = build_plans(tracks, database_paths)
        for index, plan in enumerate(plans):
            if index:
                print("\n" + "=" * 72 + "\n")
            print_plan(plan, applying=args.apply)
        if not args.apply:
            print("\nNo changes written. Review both plans, then rerun with --apply.")
            return 0
        if not any(plan["changes"] for plan in plans):
            print("\nNo changes are required.")
            return 0
        backups = apply_plans(plans, args.backup_directory)
        for database, backup in backups.items():
            print(f"\nBackup created for {Path(database).name}: {backup}")
        for plan in plans:
            print(f"Updated {len(plan['changes'])} track(s) in {plan['kind']}.")
        return 0
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
