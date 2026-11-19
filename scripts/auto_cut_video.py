"""Auto-cut unwanted video ranges and dead silence using FFmpeg (0-dependency Python stdlib)."""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = PROJECT_DIR / "media"
RAW_DIR = MEDIA_DIR / "01-raw"
AUTOCUT_DIR = MEDIA_DIR / "02-autocut"
SCRATCH_DIR = MEDIA_DIR / "_scratch"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".flv"}


def find_binary(name: str) -> str:
    """Locate ffmpeg/ffprobe on PATH, WinGet packages, or standard Windows dirs."""
    found = shutil.which(name)
    if found:
        return found

    candidates: List[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_root = Path(local_app_data) / "Microsoft" / "WinGet"
        candidates.append(winget_root / "Links" / f"{name}.exe")
        pkg_root = winget_root / "Packages"
        if pkg_root.exists():
            candidates.extend(pkg_root.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe"))

    candidates.extend([
        Path(rf"C:\ffmpeg\bin\{name}.exe"),
        Path(rf"C:\Program Files\ffmpeg\bin\{name}.exe"),
    ])

    for path in candidates:
        if path.exists():
            return str(path)

    raise FileNotFoundError(
        f"Binary '{name}' not found. Install FFmpeg first (e.g. 'winget install Gyan.FFmpeg' on Windows)."
    )


def parse_timestamp(ts: str) -> float:
    """Parse SS, MM:SS, or HH:MM:SS(.ms) into seconds."""
    parts = ts.strip().split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Invalid timestamp format: '{ts}' (expected MM:SS or HH:MM:SS)")


def parse_cut_range(spec: str) -> Tuple[float, float]:
    """Parse 'START-END' into a (start_sec, end_sec) tuple."""
    if "-" not in spec:
        raise ValueError(f"Invalid cut range: '{spec}' (example: 04:30-06:45)")
    left, right = spec.split("-", 1)
    start, end = parse_timestamp(left), parse_timestamp(right)
    if end <= start:
        raise ValueError(f"End timestamp must be greater than start timestamp: '{spec}'")
    return (start, end)


def format_ts(seconds: float) -> str:
    """Format seconds as MM:SS.s for readable logs."""
    mins = int(seconds // 60)
    secs = seconds - mins * 60
    return f"{mins:02d}:{secs:05.2f}"


def merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Merge overlapping or adjacent time intervals."""
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        prev_s, prev_e = merged[-1]
        if start <= prev_e:
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(
    keep: List[Tuple[float, float]],
    excludes: List[Tuple[float, float]],
    min_len: float = 0.25,
) -> List[Tuple[float, float]]:
    """Subtract excluded intervals from kept intervals."""
    result = keep
    for ex_s, ex_e in merge_intervals(excludes):
        next_res: List[Tuple[float, float]] = []
        for k_s, k_e in result:
            if ex_e <= k_s or ex_s >= k_e:
                next_res.append((k_s, k_e))
                continue
            if ex_s > k_s:
                next_res.append((k_s, ex_s))
            if ex_e < k_e:
                next_res.append((ex_e, k_e))
        result = next_res
    return [(s, e) for s, e in result if (e - s) >= min_len]


def get_duration(ffprobe_bin: str, video_path: Path) -> float:
    """Return total media duration in seconds via ffprobe."""
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


def detect_silence(
    ffmpeg_bin: str,
    video_path: Path,
    duration: float,
    noise_db: float,
    min_silence: float,
    pad: float,
) -> List[Tuple[float, float]]:
    """Detect silent spans using ffmpeg silencedetect and preserve speech padding."""
    cmd = [
        ffmpeg_bin,
        "-i",
        str(video_path),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    stderr = res.stderr

    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*([0-9.]+)", stderr)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([0-9.]+)", stderr)]

    raw_silences: List[Tuple[float, float]] = []
    for idx, s in enumerate(starts):
        e = ends[idx] if idx < len(ends) else duration
        cut_s = s + pad
        cut_e = e - pad
        if cut_e > cut_s + 0.05:
            raw_silences.append((cut_s, cut_e))
    return merge_intervals(raw_silences)


def cut_segment(
    ffmpeg_bin: str,
    input_path: Path,
    seg_path: Path,
    start: float,
    end: float,
) -> None:
    """Extract one frame-accurate segment with uniform encoding params for clean concat."""
    duration = end - start
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(input_path),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        str(seg_path),
    ]
    subprocess.run(cmd, check=True)


def concat_segments(
    ffmpeg_bin: str,
    seg_paths: List[Path],
    out_path: Path,
    normalize_audio: bool,
) -> None:
    """Concatenate extracted segments with optional global YouTube loudness normalization."""
    list_file = seg_paths[0].parent / "concat_list.txt"
    lines = [f"file '{p.as_posix()}'" for p in seg_paths]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c:v",
        "copy",
    ]
    if normalize_audio:
        cmd.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.extend(["-c:a", "copy"])

    cmd.extend(["-movflags", "+faststart", str(out_path)])
    subprocess.run(cmd, check=True)


def find_latest_raw_video(raw_dir: Path = RAW_DIR) -> Path:
    """Return the most recently modified video file inside media/01-raw/."""
    if not raw_dir.exists():
        raise FileNotFoundError(f"Raw media directory not found: {raw_dir}")
    candidates = [
        p for p in raw_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not candidates:
        raise FileNotFoundError(f"No video files found in: {raw_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def resolve_default_output(input_path: Path, explicit_output: Optional[str]) -> Path:
    """Route files from media/01-raw/ to media/02-autocut/, else beside input."""
    if explicit_output:
        return Path(explicit_output).expanduser().resolve()
    raw_root = RAW_DIR.resolve()
    if input_path.is_relative_to(raw_root):
        return (AUTOCUT_DIR / f"{input_path.stem}_autocut.mp4").resolve()
    return input_path.with_name(f"{input_path.stem}_edited.mp4")


def inspect_media(
    ffmpeg_bin: str,
    ffprobe_bin: str,
    input_path: Path,
    scratch_dir: Path = SCRATCH_DIR,
) -> Path:
    """Run volumedetect and export a 9x6 overview contact sheet into media/_scratch/."""
    duration = get_duration(ffprobe_bin, input_path)
    print(f"File: {input_path.name} (duration {format_ts(duration)})")

    vol_cmd = [ffmpeg_bin, "-i", str(input_path), "-af", "volumedetect", "-vn", "-f", "null", "-"]
    vol_res = subprocess.run(vol_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    mean_match = re.search(r"mean_volume:\s*([-0-9.]+)\s*dB", vol_res.stderr)
    max_match = re.search(r"max_volume:\s*([-0-9.]+)\s*dB", vol_res.stderr)
    mean_db = mean_match.group(1) if mean_match else "N/A"
    max_db = max_match.group(1) if max_match else "N/A"
    print(f"Loudness: mean_volume = {mean_db} dB | max_volume = {max_db} dB")

    scratch_dir.mkdir(parents=True, exist_ok=True)
    contact_out = (scratch_dir / f"{input_path.stem}_contact.png").resolve()
    step_sec = max(10, math.ceil(duration / 54)) if duration > 0 else 30
    cs_cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        str(input_path),
        "-vf",
        f"fps=1/{step_sec},scale=320:180,tile=9x6",
        "-frames:v",
        "1",
        str(contact_out),
    ]
    subprocess.run(cs_cmd, check=True)
    print(f"Contact Sheet (1 tile = {step_sec}s): {contact_out}")
    return contact_out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-cut manual exclusion ranges and dead silence from videos using FFmpeg."
    )
    parser.add_argument("input", nargs="?", help="Source video file (.mp4 / .mkv) or use --latest")
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Automatically select the newest video file in media/01-raw/",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Run volumedetect and export a 9x6 contact sheet into media/_scratch/ without rendering",
    )
    parser.add_argument(
        "--cut",
        action="append",
        default=[],
        help="Manual time range to exclude in MM:SS-MM:SS format (repeatable)",
    )
    parser.add_argument(
        "--remove-silence",
        action="store_true",
        help="Enable automatic silence detection and removal",
    )
    parser.add_argument("--noise-db", type=float, default=-50.0, help="Silence threshold in dB (default: -50)")
    parser.add_argument("--min-silence", type=float, default=0.9, help="Minimum silence duration to cut in seconds (default: 0.9)")
    parser.add_argument("--pad", type=float, default=0.2, help="Speech boundary padding in seconds (default: 0.2)")
    parser.add_argument("--min-segment", type=float, default=0.55, help="Minimum kept speech segment in seconds (default: 0.55 to filter clicks)")
    parser.add_argument("--normalize", action="store_true", help="Normalize output audio to YouTube loudness standard (-16 LUFS)")
    parser.add_argument("--output", help="Output file path (default: media/02-autocut/<stem>_autocut.mp4 or <stem>_edited.mp4)")
    parser.add_argument("--dry-run", action="store_true", help="Preview kept segments without rendering video")

    args = parser.parse_args()
    if args.latest and args.input:
        print("Error: Provide either an input file path or --latest, not both.", file=sys.stderr)
        return 1

    try:
        if args.latest:
            input_path = find_latest_raw_video().resolve()
            print(f"Selected latest raw video from media/01-raw/: {input_path.name}")
        elif args.input:
            input_path = Path(args.input).expanduser().resolve()
        else:
            parser.print_help()
            return 1
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        return 1

    try:
        ffmpeg_bin = find_binary("ffmpeg")
        ffprobe_bin = find_binary("ffprobe")
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.inspect:
        inspect_media(ffmpeg_bin, ffprobe_bin, input_path)
        return 0

    duration = get_duration(ffprobe_bin, input_path)
    excludes: List[Tuple[float, float]] = [parse_cut_range(c) for c in args.cut]

    if args.remove_silence:
        print("Scanning video for silent spans...")
        silences = detect_silence(
            ffmpeg_bin, input_path, duration, args.noise_db, args.min_silence, args.pad
        )
        print(f"   Found {len(silences)} removable silence spans")
        excludes.extend(silences)

    segments = subtract_intervals([(0.0, duration)], excludes, min_len=args.min_segment)
    if not segments:
        print("Error: No video segments remain after applying exclusions.", file=sys.stderr)
        return 1

    kept_total = sum(e - s for s, e in segments)
    print(f"Summary: {format_ts(duration)} -> {format_ts(kept_total)} ({len(segments)} segments)")
    for idx, (s, e) in enumerate(segments, 1):
        print(f"   [{idx:02d}] {format_ts(s)} -> {format_ts(e)} ({e - s:.2f}s)")

    if args.dry_run:
        print("Dry-run mode: skipped video rendering.")
        return 0

    out_path = resolve_default_output(input_path, args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Extracting {len(segments)} segments and rendering to: {out_path.name} ...")
    with tempfile.TemporaryDirectory(prefix="yt_autocut_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        seg_paths: List[Path] = []
        for idx, (s, e) in enumerate(segments):
            seg_file = tmp_root / f"seg_{idx:04d}.mp4"
            cut_segment(ffmpeg_bin, input_path, seg_file, s, e)
            seg_paths.append(seg_file)
            if (idx + 1) % 25 == 0 or (idx + 1) == len(segments):
                print(f"   Extracted {idx + 1}/{len(segments)} segments")

        print("Concatenating segments" + (" + normalizing audio (-16 LUFS)..." if args.normalize else "..."))
        concat_segments(ffmpeg_bin, seg_paths, out_path, normalize_audio=args.normalize)

    print(f"Done: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
