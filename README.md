# 🎬 Agentic Media & Asset Production Pipeline

Deterministic, zero-external-Python-dependency media automation tools built for rapid content operations:

1. **FFmpeg Audio/Video Auto-Cut & Loudness Normalization (`scripts/auto_cut_video.py`):**
   - Uses FFmpeg `volumedetect` / `silencedetect` (`-50 dB` threshold) + minimum speech segment filtering (`--min-segment 0.55`) to strip dead air and mouse-click noise.
   - Supports manual exclusion ranges (`--cut MM:SS-MM:SS`), chunked `concat` demuxer assembly (low RAM usage on FFmpeg 9.x), and `-16 LUFS` broadcast loudness normalization.
   - Reduced raw 27-minute recordings down to clean ~10-minute edits automatically.

## 🚀 Usage (`auto_cut_video.py`)

Requires Python 3.9+ and `ffmpeg` / `ffprobe` installed (`winget install Gyan.FFmpeg` on Windows).

```bash
# Inspect silence intervals without modifying video
python scripts/auto_cut_video.py input.mp4 --inspect

# Auto-cut silence and normalize loudness to -16 LUFS
python scripts/auto_cut_video.py input.mp4 --normalize

# Cut specific manual ranges in addition to silence
python scripts/auto_cut_video.py input.mp4 --cut 04:30-06:45 --normalize
```

## 📁 Structure
- `scripts/auto_cut_video.py` — Zero-dependency Python CLI wrapping `ffmpeg` & `ffprobe`
