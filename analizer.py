#!/usr/bin/env python3

import argparse
import json
import math
import sys
import unicodedata
from pathlib import Path


AUDIO_EXTENSIONS = frozenset({".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".wav"})
CAMELOT_MINOR = ("5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A")
CAMELOT_MAJOR = ("8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B")


def _dependencies():
    try:
        import librosa
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Missing audio dependencies. Install them with: python3 -m pip install librosa soundfile"
        ) from exc
    return librosa, np


def _normalize_rekordbox_path(value):
    path = "/" + str(value).replace("\\", "/").lstrip("/")
    return unicodedata.normalize("NFC", path).casefold()


def _normalize_camelot(value):
    key = str(value or "").strip().upper()
    if len(key) not in (2, 3) or key[-1] not in "AB":
        return None
    try:
        number = int(key[:-1])
    except ValueError:
        return None
    return key if 1 <= number <= 12 else None


def _find_rekordbox_database(path):
    start = Path(path).expanduser()
    if start.is_file():
        start = start.parent
    for root in (start, *start.parents):
        database = root / "PIONEER" / "rekordbox" / "exportLibrary.db"
        if database.is_file():
            return root, database
    return None, None


def _load_rekordbox_metadata(path):
    usb_root, database_path = _find_rekordbox_database(path)
    info = {
        "database": str(database_path.resolve()) if database_path else None,
        "entry_count": 0,
        "matched_count": 0,
        "error": None,
    }
    if database_path is None:
        return {}, usb_root, info

    try:
        from pyrekordbox import DeviceLibraryPlus

        metadata = {}
        with DeviceLibraryPlus(database_path) as database:
            for content in database.get_content():
                bpm_value = int(content.bpmx100 or 0)
                camelot = _normalize_camelot(content.key.name if content.key else None)
                metadata[_normalize_rekordbox_path(content.path)] = {
                    "bpm": round(bpm_value / 100.0, 2) if bpm_value > 0 else None,
                    "camelot": camelot,
                }
        info["entry_count"] = len(metadata)
        return metadata, usb_root, info
    except Exception as exc:
        info["error"] = str(exc)
        return {}, usb_root, info


def _rekordbox_match(audio_file, usb_root, metadata):
    if usb_root is None:
        return None
    try:
        relative_path = audio_file.resolve().relative_to(usb_root.resolve())
    except ValueError:
        return None
    return metadata.get(_normalize_rekordbox_path(relative_path))


def _scale(value, low, high):
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _analyze_tempo(y, sample_rate, librosa, np):
    hop_length = 512
    onset_envelope = librosa.onset.onset_strength(
        y=y, sr=sample_rate, hop_length=hop_length
    )
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        hop_length=hop_length,
        units="frames",
    )
    bpm = float(np.asarray(tempo).reshape(-1)[0])
    if len(beat_frames) < 4:
        confidence = 0.0
    else:
        intervals = np.diff(beat_frames)
        median_interval = float(np.median(intervals))
        stable_intervals = intervals[
            np.abs(intervals - median_interval) <= max(1.0, median_interval * 0.25)
        ]
        if len(stable_intervals):
            bpm = 60.0 * sample_rate / (hop_length * float(np.mean(stable_intervals)))
        regularity = math.exp(
            -4.0 * float(np.std(intervals) / max(np.mean(intervals), 1e-10))
        )
        beat_strength = float(np.median(onset_envelope[beat_frames]))
        reference_strength = float(np.percentile(onset_envelope, 90))
        strength = min(1.0, beat_strength / max(reference_strength, 1e-10))
        confidence = 100.0 * (0.65 * regularity + 0.35 * strength)

    while 0 < bpm < 70:
        bpm *= 2.0
    while bpm > 180:
        bpm /= 2.0

    return {
        "bpm": round(bpm, 1),
        "confidence": round(confidence, 1),
    }


def _key_candidate(root, mode, score):
    camelot = CAMELOT_MAJOR[root] if mode == "major" else CAMELOT_MINOR[root]
    return {
        "camelot": camelot,
        "score": score,
    }


def _analyze_key(y, sample_rate, librosa, np):
    hop_length = 2048
    chroma = librosa.feature.chroma_cqt(
        y=y, sr=sample_rate, hop_length=hop_length
    )
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    frame_count = min(chroma.shape[1], len(rms))
    chroma = chroma[:, :frame_count]
    rms = rms[:frame_count]
    active = rms >= np.percentile(rms, 25)
    if np.any(active):
        chroma = chroma[:, active]
        rms = rms[active]
    pitch_profile = np.average(chroma, axis=1, weights=np.maximum(rms, 1e-10))
    pitch_profile /= max(float(np.sum(pitch_profile)), 1e-10)

    major_profile = np.array(
        [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
    )
    minor_profile = np.array(
        [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
    )
    centered_pitch = pitch_profile - np.mean(pitch_profile)
    pitch_norm = max(float(np.linalg.norm(centered_pitch)), 1e-10)
    candidates = []
    for root in range(12):
        for mode, template in (("major", major_profile), ("minor", minor_profile)):
            shifted = np.roll(template, root)
            centered_template = shifted - np.mean(shifted)
            correlation = float(
                np.dot(centered_pitch, centered_template)
                / (pitch_norm * max(float(np.linalg.norm(centered_template)), 1e-10))
            )
            candidates.append(_key_candidate(root, mode, correlation))

    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    correlations = np.array([candidate["score"] for candidate in candidates])
    probabilities = np.exp((correlations - correlations[0]) / 0.12)
    probabilities /= np.sum(probabilities)
    best = candidates[0]
    alternative = candidates[1]
    return {
        "camelot": best["camelot"],
        "confidence": round(float(probabilities[0] * 100.0), 1),
        "alternative": {
            "camelot": alternative["camelot"],
            "confidence": round(float(probabilities[1] * 100.0), 1),
        },
    }


def _extract_features(y, sample_rate, librosa, np):
    hop_length = 512
    frame_length = 2048
    magnitude = np.abs(
        librosa.stft(y, n_fft=frame_length, hop_length=hop_length)
    )
    power = magnitude**2
    rms_frames = librosa.feature.rms(
        y=y, frame_length=frame_length, hop_length=hop_length
    )[0]
    rms = float(np.percentile(rms_frames, 75))
    rms_dbfs = float(20.0 * np.log10(max(rms, 1e-10)))

    signal_rms = float(np.sqrt(np.mean(y**2)))
    robust_peak = float(np.percentile(np.abs(y), 99.9))
    crest_db = float(20.0 * np.log10(max(robust_peak, 1e-10) / max(signal_rms, 1e-10)))

    frequencies = librosa.fft_frequencies(sr=sample_rate, n_fft=frame_length)
    spectral_total = np.maximum(np.sum(magnitude, axis=0), 1e-10)
    centroid = np.sum(frequencies[:, None] * magnitude, axis=0) / spectral_total
    centroid_hz = float(np.median(centroid))
    brightness_ratio = centroid_hz / (sample_rate / 2.0)

    bass_bins = frequencies <= 250.0
    bass_ratio = float(
        np.median(
            np.sum(power[bass_bins], axis=0)
            / np.maximum(np.sum(power, axis=0), 1e-10)
        )
    )

    onset_envelope = librosa.onset.onset_strength(
        S=librosa.amplitude_to_db(magnitude, ref=np.max),
        sr=sample_rate,
        hop_length=hop_length,
    )
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        hop_length=hop_length,
        units="frames",
    )
    duration = len(y) / sample_rate
    onset_rate = float(len(onset_frames) / max(duration, 1e-10))

    components = {
        "loudness": _scale(rms_dbfs, -32.0, -8.0),
        "compression": _scale(crest_db, 18.0, 6.0),
        "rhythmic_activity": _scale(onset_rate, 0.3, 3.5),
        "brightness": _scale(brightness_ratio, 0.04, 0.16),
        "bass_weight": _scale(bass_ratio, 0.08, 0.40),
    }
    score = 100.0 * (
        0.35 * components["loudness"]
        + 0.15 * components["compression"]
        + 0.25 * components["rhythmic_activity"]
        + 0.10 * components["brightness"]
        + 0.15 * components["bass_weight"]
    )

    return {
        "score": round(score, 1),
        "features": {
            "rms_dbfs": round(rms_dbfs, 2),
            "crest_factor_db": round(crest_db, 2),
            "onsets_per_second": round(onset_rate, 3),
            "spectral_centroid_hz": round(centroid_hz, 1),
            "bass_power_ratio": round(bass_ratio, 4),
        },
        "components": {
            name: round(value * 100.0, 1) for name, value in components.items()
        },
    }


def _energy_label(score):
    if score < 20:
        return "very low"
    if score < 40:
        return "low"
    if score < 60:
        return "medium"
    if score < 80:
        return "high"
    return "peak"


def analyze_track(path, segment_seconds=30.0, sample_rate=22050, rekordbox=None):
    audio_path = Path(path).expanduser()
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if 0 < segment_seconds < 2:
        raise ValueError("segment_seconds must be 0 or at least 2")

    librosa, np = _dependencies()
    y, loaded_sample_rate = librosa.load(
        audio_path, sr=sample_rate, mono=True, dtype=np.float32
    )
    if len(y) < loaded_sample_rate:
        raise ValueError("The audio file must contain at least one second of audio")
    if not np.any(np.abs(y) > 1e-8):
        raise ValueError("The audio file is silent")

    analysis = _extract_features(y, loaded_sample_rate, librosa, np)
    rekordbox_bpm = rekordbox.get("bpm") if rekordbox else None
    rekordbox_key = rekordbox.get("camelot") if rekordbox else None
    if rekordbox_bpm:
        tempo = {
            "bpm": rekordbox_bpm,
            "confidence": None,
            "source": "rekordbox",
        }
    else:
        tempo = _analyze_tempo(y, loaded_sample_rate, librosa, np)
        tempo["source"] = "librosa"
    if rekordbox_key:
        key = {
            "camelot": rekordbox_key,
            "confidence": None,
            "alternative": None,
            "source": "rekordbox",
        }
    else:
        key = _analyze_key(y, loaded_sample_rate, librosa, np)
        key["source"] = "librosa"
    score = analysis["score"]
    result = {
        "file": str(audio_path.resolve()),
        "duration_seconds": round(len(y) / loaded_sample_rate, 2),
        "analysis_sample_rate": loaded_sample_rate,
        "tempo": tempo,
        "key": key,
        "energy": {
            "score": score,
            "dj_level": max(1, min(10, math.ceil(score / 10.0))),
            "label": _energy_label(score),
            "source": "librosa",
        },
        "features": analysis["features"],
        "components": analysis["components"],
        "segments": [],
    }

    if segment_seconds > 0:
        segment_samples = int(segment_seconds * loaded_sample_rate)
        for start in range(0, len(y), segment_samples):
            segment = y[start : start + segment_samples]
            if len(segment) < loaded_sample_rate * 2:
                continue
            segment_analysis = _extract_features(
                segment, loaded_sample_rate, librosa, np
            )
            segment_score = segment_analysis["score"]
            result["segments"].append(
                {
                    "start_seconds": round(start / loaded_sample_rate, 2),
                    "end_seconds": round(
                        min(start + len(segment), len(y)) / loaded_sample_rate, 2
                    ),
                    "score": segment_score,
                    "dj_level": max(1, min(10, math.ceil(segment_score / 10.0))),
                    "label": _energy_label(segment_score),
                }
            )

    return result


def _discover_audio_files(directory):
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and not any(part.startswith(".") for part in path.relative_to(directory).parts)
    )


def _add_collection_ranking(tracks):
    scores = [track["energy"]["score"] for track in tracks]
    total = len(scores)
    for track in tracks:
        score = track["energy"]["score"]
        rank = 1 + sum(other > score for other in scores)
        percentile = 100.0 * sum(other <= score for other in scores) / total
        track["collection"] = {
            "rank": rank,
            "percentile": round(percentile, 1),
            "dj_level": max(1, min(10, math.ceil(percentile / 10.0))),
        }
    tracks.sort(key=lambda track: (-track["energy"]["score"], track["file"].lower()))


def analyze_directory(path, segment_seconds=30.0, sample_rate=22050, progress=None):
    directory = Path(path).expanduser()
    if not directory.is_dir():
        raise NotADirectoryError(f"Directory not found: {directory}")

    audio_files = _discover_audio_files(directory)
    if not audio_files:
        supported = ", ".join(sorted(AUDIO_EXTENSIONS))
        raise ValueError(f"No supported audio files found. Supported extensions: {supported}")

    rekordbox_metadata, usb_root, rekordbox_info = _load_rekordbox_metadata(directory)
    tracks = []
    errors = []
    for index, audio_file in enumerate(audio_files, start=1):
        if progress:
            progress(index, len(audio_files), audio_file)
        try:
            rekordbox = _rekordbox_match(audio_file, usb_root, rekordbox_metadata)
            if rekordbox:
                rekordbox_info["matched_count"] += 1
            track = analyze_track(
                audio_file,
                segment_seconds=segment_seconds,
                sample_rate=sample_rate,
                rekordbox=rekordbox,
            )
            track["relative_file"] = str(audio_file.relative_to(directory))
            tracks.append(track)
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(
                {
                    "file": str(audio_file.resolve()),
                    "error": str(exc),
                }
            )

    if not tracks:
        raise ValueError(f"None of the {len(audio_files)} audio files could be analyzed")

    _add_collection_ranking(tracks)
    return {
        "directory": str(directory.resolve()),
        "recursive": True,
        "track_count": len(tracks),
        "error_count": len(errors),
        "rekordbox": rekordbox_info,
        "tracks": tracks,
        "errors": errors,
    }


def _print_summary(result):
    energy = result["energy"]
    features = result["features"]
    print(f"Track: {result['file']}")
    print(f"Duration: {result['duration_seconds']:.2f} s")
    print(
        f"Energy: {energy['score']:.1f}/100 | DJ level {energy['dj_level']}/10 | {energy['label']}"
    )
    tempo = result["tempo"]
    tempo_confidence = (
        f" | confidence {tempo['confidence']:.1f}%"
        if tempo["confidence"] is not None
        else ""
    )
    print(
        f"Tempo: {tempo['bpm']:.1f} BPM | source {tempo['source']}{tempo_confidence}"
    )
    key = result["key"]
    key_confidence = (
        f" | confidence {key['confidence']:.1f}%"
        if key["confidence"] is not None
        else ""
    )
    print(f"Key: {key['camelot']} | source {key['source']}{key_confidence}")
    alternative = key["alternative"]
    if alternative:
        print(
            f"Alternative key: {alternative['camelot']} | "
            f"confidence {alternative['confidence']:.1f}%"
        )
    print(
        "Features: "
        f"RMS {features['rms_dbfs']:.2f} dBFS, "
        f"crest {features['crest_factor_db']:.2f} dB, "
        f"onsets {features['onsets_per_second']:.3f}/s, "
        f"centroid {features['spectral_centroid_hz']:.1f} Hz, "
        f"bass ratio {features['bass_power_ratio']:.3f}"
    )
    if result["segments"]:
        curve = "  ".join(
            f"{segment['start_seconds']:.0f}s:{segment['score']:.0f}"
            for segment in result["segments"]
        )
        print(f"Energy curve: {curve}")


def _print_collection(result):
    print(f"Collection: {result['directory']}")
    print(
        f"Analyzed: {result['track_count']} track(s) | Errors: {result['error_count']}"
    )
    rekordbox = result["rekordbox"]
    if rekordbox["database"]:
        print(
            f"Rekordbox: {rekordbox['matched_count']} matched / "
            f"{rekordbox['entry_count']} database entries"
        )
    else:
        print("Rekordbox: database not found; using Librosa metadata")
    if rekordbox["error"]:
        print(f"Rekordbox warning: {rekordbox['error']}", file=sys.stderr)
    print()
    print(
        f"{'Rank':>4}  {'Relative':>8}  {'Score':>6}  {'BPM':>6}  "
        f"{'BPM source':<10}  {'Key':<3}  {'Key source':<10}  Track"
    )
    print(
        f"{'----':>4}  {'--------':>8}  {'-----':>6}  {'---':>6}  "
        f"{'----------':<10}  {'---':<3}  {'----------':<10}  -----"
    )
    for track in result["tracks"]:
        collection = track["collection"]
        energy = track["energy"]
        print(
            f"{collection['rank']:>4}  "
            f"{collection['dj_level']:>7}/10  "
            f"{energy['score']:>6.1f}  "
            f"{track['tempo']['bpm']:>6.1f}  "
            f"{track['tempo']['source']:<10}  "
            f"{track['key']['camelot']:<3}  "
            f"{track['key']['source']:<10}  "
            f"{track['relative_file']}"
        )
    if result["errors"]:
        print("\nErrors:", file=sys.stderr)
        for error in result["errors"]:
            print(f"- {error['file']}: {error['error']}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Estimate DJ-oriented energy for one track or a recursive audio collection."
    )
    parser.add_argument("audio_path", help="Path to an audio file or directory")
    parser.add_argument(
        "--segment-seconds",
        type=float,
        default=30.0,
        help="Energy-curve segment length; use 0 to disable (default: 30)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=22050,
        help="Analysis sample rate (default: 22050)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    if args.segment_seconds < 0:
        parser.error("--segment-seconds cannot be negative")
    if 0 < args.segment_seconds < 2:
        parser.error("--segment-seconds must be 0 or at least 2")
    if args.sample_rate < 8000:
        parser.error("--sample-rate must be at least 8000")

    audio_path = Path(args.audio_path).expanduser()
    try:
        if audio_path.is_dir():
            result = analyze_directory(
                audio_path,
                segment_seconds=args.segment_seconds,
                sample_rate=args.sample_rate,
                progress=lambda index, total, path: print(
                    f"Analyzing [{index}/{total}] {path.name}", file=sys.stderr
                ),
            )
        elif audio_path.is_file():
            rekordbox_metadata, usb_root, rekordbox_info = _load_rekordbox_metadata(
                audio_path
            )
            rekordbox = _rekordbox_match(audio_path, usb_root, rekordbox_metadata)
            if rekordbox:
                rekordbox_info["matched_count"] = 1
            result = analyze_track(
                audio_path,
                segment_seconds=args.segment_seconds,
                sample_rate=args.sample_rate,
                rekordbox=rekordbox,
            )
            result["rekordbox"] = rekordbox_info
        else:
            raise FileNotFoundError(f"Audio file or directory not found: {audio_path}")
    except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    elif audio_path.is_dir():
        _print_collection(result)
    else:
        _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
