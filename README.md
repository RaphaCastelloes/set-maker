# Set Maker

Analyze DJ tracks for energy, BPM, and Camelot key, rank tracks relative to a collection, and optionally update metadata in both Rekordbox USB database formats.

## Features

- Analyze one audio file or a directory recursively.
- Calculate a DJ-oriented energy score and 30-second energy curve.
- Rank energy from 1 to 10 relative to the analyzed collection.
- Read BPM and key from a Rekordbox USB database when available.
- Fall back to Librosa when Rekordbox BPM or key is missing.
- Normalize Rekordbox musical notation and Camelot notation to Camelot output.
- Export analysis as JSON.
- Preview and apply artist/album naming conventions to Device Library Plus `exportLibrary.db` and classic Device Library `export.pdb`.
- Create timestamped backups of both databases before applying updates.

Supported audio extensions are `.wav`, `.aif`, `.aiff`, `.flac`, `.m4a`, `.mp3`, and `.ogg`.

## Setup

This project uses a Python virtual environment.

```bash
sudo apt install python3.14-venv
python3 -m venv .venv
.venv/bin/python -m pip install "librosa==0.11.0" "soundfile==0.13.1"
.venv/bin/python -m pip install "pyrekordbox @ git+https://github.com/dylanljones/pyrekordbox.git@f695541827cc488af267d6ca8a8e0052598d85a0"
.venv/bin/python -m pip install "rekordbox-pdb @ git+https://github.com/fragmede/rekordbox-pdb.git@ee3bac2f22ca11a5ce61eea35f8cb951c246eaef"
```

The pinned Pyrekordbox revision provides Device Library Plus support for `exportLibrary.db`. The pinned `rekordbox-pdb` revision provides read/write support for the classic Device Library `export.pdb` format.

Commands below assume the terminal is in the project directory:

```bash
cd /home/rapha/Repos/set-maker
```

## Analyze one track

```bash
.venv/bin/python analizer.py "track.wav"
```

Example with the local track:

```bash
.venv/bin/python analizer.py "MBNN & Rowald Steyn - ilomilo (Extended Mix).wav"
```

Machine-readable output:

```bash
.venv/bin/python analizer.py "track.wav" --json
```

## Analyze a directory recursively

Analyze the current directory:

```bash
.venv/bin/python analizer.py .
```

Analyze the complete CASTELLO USB:

```bash
.venv/bin/python analizer.py "/run/media/rapha/CASTELLO"
```

Hidden directories such as `.venv` are ignored.

## Faster collection analysis

The default analysis calculates an energy curve in 30-second segments. Disable the curve to process a collection faster:

```bash
.venv/bin/python analizer.py "/run/media/rapha/CASTELLO" --segment-seconds 0
```

Use a different segment duration:

```bash
.venv/bin/python analizer.py "/run/media/rapha/CASTELLO" --segment-seconds 15
```

Use a different analysis sample rate:

```bash
.venv/bin/python analizer.py "track.wav" --sample-rate 44100
```

## Save collection analysis as JSON

The updater requires collection JSON containing relative energy levels. Save it in the project root:

```bash
.venv/bin/python analizer.py "/run/media/rapha/CASTELLO" \
  --segment-seconds 0 \
  --json > castello-analysis.json
```

To organize reports in a folder, create the folder first and redirect the output to a file inside it:

```bash
mkdir -p reports

.venv/bin/python analizer.py "/run/media/rapha/CASTELLO" \
  --segment-seconds 0 \
  --json > "reports/pendrive-analysis.json"
```

The output directory must already exist. Shell redirection creates or replaces the JSON file, but it does not create parent directories. Progress is printed to standard error, so only valid JSON is saved in the report file.

Use the same path when running the updater:

```bash
.venv/bin/python updater.py "reports/pendrive-analysis.json"
```

## Rekordbox metadata fallback

When analyzing a Rekordbox USB, the analyzer searches parent directories for:

```text
PIONEER/rekordbox/exportLibrary.db
```

For every matching track:

1. Rekordbox BPM is preferred when present.
2. Rekordbox key is preferred and normalized to Camelot.
3. Librosa calculates an independently missing BPM or key.
4. Librosa always calculates energy.

Example output:

```text
Score     BPM  BPM source  Key  Key source  Track
 96.1   130.0  rekordbox   9A   rekordbox   track.wav
 93.0   118.3  librosa     8B   librosa     new-track.wav
```

Accepted Rekordbox key forms include Camelot and musical notation:

```text
8A, Am, A minor      -> 8A
8B, C, C major      -> 8B
11A, F#m, Gb minor  -> 11A
```

## Metadata naming convention

The updater writes these values to both the Device Library Plus and classic Device Library databases:

```text
Album:  BPM|CAMELOT|RELATIVE
Artist: CAMELOT|RELATIVE Artist
```

Example:

```text
Album:  122|3A|3
Artist: 1A|3 HUGEL
```

Existing prefixes such as `1A HUGEL`, `1A* HUGEL`, and `1A|3 HUGEL` are replaced rather than duplicated. If Rekordbox has no artist relationship, the updater attempts to extract the artist from an `Artist - Title` track title or filename.

## Preview Rekordbox updates

Always run the updater without `--apply` first. This previews both databases and does not modify either one:

```bash
.venv/bin/python updater.py castello-analysis.json
```

The preview reports:

- Planned changes
- Already unchanged tracks
- Tracks absent from either database
- Invalid analysis entries
- Every old and new artist/album value

If the JSON does not contain a database path, provide either database explicitly. The updater locates the other database in the same directory:

```bash
.venv/bin/python updater.py castello-analysis.json \
  --database "/run/media/rapha/CASTELLO/PIONEER/rekordbox/exportLibrary.db"
```

Both `exportLibrary.db` and `export.pdb` must exist under `PIONEER/rekordbox`.

## Apply Rekordbox updates

After reviewing the complete dry-run output, apply the changes:

```bash
.venv/bin/python updater.py castello-analysis.json --apply
```

The updater creates backups of both databases before committing:

```text
PIONEER/rekordbox/exportLibrary.db.backup-YYYYMMDD-HHMMSS-ffffff
PIONEER/rekordbox/export.pdb.backup-YYYYMMDD-HHMMSS-ffffff
```

If either update fails, both databases are restored from these backups.

Store the backup in another existing directory if desired:

```bash
.venv/bin/python updater.py castello-analysis.json \
  --apply \
  --backup-directory "/path/to/backups"
```

## Important Rekordbox limitations

- The updater requires and updates both Device Library Plus `exportLibrary.db` and classic Device Library `export.pdb`.
- Each database receives its own dry-run plan and backup.
- Tracks absent from either database cannot be updated in that database.
- A later Rekordbox synchronization or re-export can overwrite USB changes.
- Safely eject the USB after updating it.
- Inspect the USB under **Devices** in Rekordbox before synchronizing or exporting again.
- Keep the automatic backup until the updated library has been verified.

## CLI reference

Analyzer help:

```bash
.venv/bin/python analizer.py --help
```

Updater help:

```bash
.venv/bin/python updater.py --help
```
