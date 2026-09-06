---
name: set-maker
description: Analyze DJ tracks for energy, BPM, and Camelot key, export collection results, and safely update both Rekordbox USB database formats.
---

# Set Maker

Use this project to analyze individual tracks or audio collections and, when requested, update metadata in both the Device Library Plus and classic Device Library databases on a Rekordbox USB.

## Run commands from the project root

```bash
cd /home/rapha/Repos/set-maker
```

Use the virtual environment's Python executable for every command:

```bash
.venv/bin/python
```

## Set up the environment

If `.venv` is missing, install and configure the dependencies:

```bash
sudo apt install python3.14-venv
python3 -m venv .venv
.venv/bin/python -m pip install "librosa==0.11.0" "soundfile==0.13.1"
.venv/bin/python -m pip install "pyrekordbox @ git+https://github.com/dylanljones/pyrekordbox.git@f695541827cc488af267d6ca8a8e0052598d85a0"
.venv/bin/python -m pip install "rekordbox-pdb @ git+https://github.com/fragmede/rekordbox-pdb.git@ee3bac2f22ca11a5ce61eea35f8cb951c246eaef"
```

The pinned Pyrekordbox revision is required for Device Library Plus `exportLibrary.db` support. The pinned `rekordbox-pdb` revision is required to update classic Device Library `export.pdb`.

## Analyze audio

Analyze one track:

```bash
.venv/bin/python analizer.py "/path/to/track.wav"
```

Analyze all supported audio files in a directory recursively:

```bash
.venv/bin/python analizer.py "/path/to/music"
```

Supported extensions are `.wav`, `.aif`, `.aiff`, `.flac`, `.m4a`, `.mp3`, and `.ogg`. Hidden directories are ignored.

Produce machine-readable JSON:

```bash
.venv/bin/python analizer.py "/path/to/track.wav" --json
```

The default analysis calculates an energy curve in 30-second segments. For faster collection analysis, disable it:

```bash
.venv/bin/python analizer.py "/path/to/music" --segment-seconds 0
```

Use `--segment-seconds N` to select another segment length or `--sample-rate N` to select another analysis sample rate.

When analyzing tracks on a Rekordbox USB, the analyzer searches the target's parent directories for `PIONEER/rekordbox/exportLibrary.db`. It prefers BPM and key from Rekordbox when present, falls back independently to Librosa for missing values, and always calculates energy with Librosa.

## Generate collection JSON for the updater

The updater requires collection JSON containing relative energy levels. Generate it with:

```bash
.venv/bin/python analizer.py "/path/to/rekordbox-usb" \
  --segment-seconds 0 \
  --json > collection-analysis.json
```

Progress is written to standard error, so redirected standard output contains only JSON.

## Preview Rekordbox metadata updates

Always preview changes before applying them:

```bash
.venv/bin/python updater.py collection-analysis.json
```

The dry run prints a separate plan for Device Library Plus `exportLibrary.db` and classic Device Library `export.pdb`, including planned changes, unchanged tracks, missing tracks, invalid entries, and old/new artist and album values. It does not modify either database.

If the JSON does not identify a database, provide either one explicitly; the updater locates the other in the same directory:

```bash
.venv/bin/python updater.py collection-analysis.json \
  --database "/path/to/usb/PIONEER/rekordbox/exportLibrary.db"
```

The updater formats metadata as:

```text
Album:  BPM|CAMELOT|RELATIVE
Artist: CAMELOT|RELATIVE Artist
```

Both databases must exist under `PIONEER/rekordbox`. Existing Camelot/energy prefixes are replaced rather than duplicated.

## Apply Rekordbox metadata updates

Only after reviewing the complete dry-run output, apply the changes:

```bash
.venv/bin/python updater.py collection-analysis.json --apply
```

To store the backup in another existing directory:

```bash
.venv/bin/python updater.py collection-analysis.json \
  --apply \
  --backup-directory "/path/to/backups"
```

Before committing, the updater creates timestamped backups named like `exportLibrary.db.backup-YYYYMMDD-HHMMSS-ffffff` and `export.pdb.backup-YYYYMMDD-HHMMSS-ffffff`. If either update fails, it restores both databases. Keep the backups until both updated libraries have been verified.

## Safety and limitations

- Never add `--apply` before the user has reviewed both dry-run plans.
- Require both Device Library Plus `exportLibrary.db` and classic Device Library `export.pdb`.
- Tracks absent from either database cannot be updated in that database.
- A later Rekordbox synchronization or re-export can overwrite USB changes.
- After updating, safely eject the USB and inspect it under **Devices** in Rekordbox before synchronizing or exporting again.

## CLI help

```bash
.venv/bin/python analizer.py --help
.venv/bin/python updater.py --help
```
