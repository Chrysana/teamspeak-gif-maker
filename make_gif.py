# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert video clips (or GIFs) into GIFs sized for TeamSpeak avatars.

Pipeline: ffmpeg (trim, match fps, lanczos downscale, one shared colour
palette for the whole clip) -> gifsicle -O3 (store only changed pixels).

TeamSpeak 6 rejects GIFs with per-frame ("local") colour tables with an
"invalid size" error, so every frame must share the single global palette.
That rules out gifski, whose output always uses per-frame palettes.

Usage:
    python make_gif.py clip.mp4 [more clips...] [options]
    (or drag files onto "Make GIF.bat")
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

__version__ = "0.9.1"

BIN = Path(__file__).resolve().parent / "bin"


def find_tool(name: str, subdir: str) -> str | None:
    """Prefer a copy in bin/<subdir>/ (or bin/), otherwise fall back to PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    for candidate in (BIN / subdir / exe, BIN / exe):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


FFMPEG = find_tool("ffmpeg", "ffmpeg")
FFPROBE = find_tool("ffprobe", "ffmpeg")
GIFSICLE = find_tool("gifsicle", "gifsicle")

# GIF frame delays are whole 1/100ths of a second, so 50fps (2/100s) is the
# fastest rate that can be stored exactly. Faster sources are resampled to this.
MAX_GIF_FPS = 50
# --force60: keep up to 60fps using alternating 2/100s and 1/100s delays. Correct on
# paper, but most viewers (browsers, Chromium-based UIs) slow 1/100s frames to
# 10/100s, so playback may stutter.
FORCE60_FPS = 60

DITHERS = {"none": "none", "atkinson": "atkinson", "floyd": "floyd_steinberg",
           "sierra": "sierra2_4a", "bayer": "bayer:bayer_scale=4"}

# preset -> (palette colours, dither, gifsicle lossy level, max fps). Measured on test clips.
PRESETS = {
    # Atkinson spreads only 3/4 of the colour error, so it softens banding without the
    # speckle Floyd-Steinberg adds to dark flat areas. Same size as standard.
    "high":     (256, "atkinson", 0, MAX_GIF_FPS),
    "standard": (256, "none", 0, MAX_GIF_FPS),   # most accurate per byte; faint banding
    "small":    (128, "none", 30, MAX_GIF_FPS),  # ~20-35% smaller; visible banding
    "smallest": (128, "none", 30, 30),           # small + max 30fps
}


def probe(path: Path) -> dict:
    out = subprocess.run(
        [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
         "-count_packets",
         "-show_entries",
         "stream=width,height,avg_frame_rate,r_frame_rate,nb_read_packets:format=duration,format_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    if not data.get("streams"):
        raise ValueError("no video stream found")
    s = data["streams"][0]
    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0) or 0)
    frames = int(s.get("nb_read_packets", 0) or 0)
    if fmt.get("format_name") == "gif" and frames and duration:
        # GIF frame delays vary (e.g. 3/3/4 hundredths for 30fps), so ffprobe's
        # reported rate is unreliable. Average over the whole file instead.
        fps = frames / duration
    else:
        rate = s.get("avg_frame_rate", "0/0")
        if rate in ("0/0", "0/1"):
            rate = s.get("r_frame_rate", "25/1")
        fps = float(Fraction(rate))
    return {
        "width": int(s["width"]),
        "height": int(s["height"]),
        "fps": round(fps, 3),
        "duration": duration,
        "is_gif": fmt.get("format_name") == "gif",
    }


def local_palette_count(path: Path) -> int:
    """Number of frames carrying their own colour table (TeamSpeak rejects these)."""
    d = path.read_bytes()
    flags = d[10]
    i = 13 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)
    count = 0
    while i < len(d):
        b = d[i]
        if b == 0x21:  # extension block
            i += 2
            while d[i]:
                i += d[i] + 1
            i += 1
        elif b == 0x2C:  # image descriptor
            fl = d[i + 9]
            if fl & 0x80:
                count += 1
            i += 10 + (3 * 2 ** ((fl & 7) + 1) if fl & 0x80 else 0) + 1
            while d[i]:
                i += d[i] + 1
            i += 1
        else:  # trailer
            break
    return count


def build_filter(fps: float, size: int, square: bool, colors: int, dither: str) -> str:
    parts = [f"fps={fps:g}"]
    if square:
        parts.append("crop='min(iw,ih)':'min(iw,ih)'")
    # Fit inside size x size, keep aspect ratio, never upscale.
    parts.append(f"scale='min({size},iw)':'min({size},ih)':force_original_aspect_ratio=decrease"
                 ":flags=lanczos+accurate_rnd+full_chroma_int")
    # One palette for the whole clip, built from every frame.
    parts.append("split[a][b];[a]palettegen=stats_mode=full"
                 f":max_colors={colors}[p];[b][p]paletteuse=dither={DITHERS[dither]}"
                 ":diff_mode=rectangle")
    return ",".join(parts)


def convert(src: Path, args) -> Path:
    if not src.is_file():
        raise FileNotFoundError("file not found")
    info = probe(src)
    cap = PRESETS[args.preset][3]
    if args.force60 and cap == MAX_GIF_FPS:
        cap = FORCE60_FPS
    fps = args.fps or min(info["fps"], cap)
    out = Path(args.output) if args.output else src.with_suffix(".gif")
    if out.resolve() == src.resolve():
        # Input is already a GIF: never overwrite the file we're reading from.
        out = src.with_name(f"{src.stem}_ts.gif")

    print(f"{src.name}: {info['width']}x{info['height']} @ {info['fps']:g}fps"
          f" -> GIF @ {fps:g}fps, {args.colors} colours, dither {args.dither}"
          + (f", lossy {args.lossy}" if args.lossy else ""))
    if fps > MAX_GIF_FPS:
        print("  note: --force60 uses 1/100s frame delays, which many viewers slow down;"
              " if it stutters, drop --force60")

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.gif"
        ff = [str(FFMPEG), "-v", "error", "-y",
              # high-quality scaling for automatic pixel-format conversions too
              "-sws_flags", "lanczos+accurate_rnd+full_chroma_int"]
        if args.start:
            ff += ["-ss", args.start]
        if args.end:
            ff += ["-to", args.end]
        ff += ["-i", str(src)]
        if args.duration:
            ff += ["-t", args.duration]
        ff += ["-an", "-lavfi",
               build_filter(fps, args.size, args.square, args.colors, args.dither),
               "-loop", "0", str(raw)]
        subprocess.run(ff, check=True)
        gs = [str(GIFSICLE), "--no-warnings", "-O3"]
        if args.lossy:
            gs.append(f"--lossy={args.lossy}")
        subprocess.run(gs + [str(raw), "-o", str(out)], check=True)

    res = probe(out)
    kb = out.stat().st_size / 1024
    size_str = f"{kb / 1024:.2f} MB" if kb >= 1024 else f"{kb:.0f} KB"
    print(f"  -> {out}  {res['width']}x{res['height']}, {res['duration']:.2f}s, {size_str}")
    if local_palette_count(out):
        print("  !! warning: output has per-frame colour tables; TeamSpeak will reject it")
    if info["is_gif"] and out.stat().st_size > src.stat().st_size:
        print("  note: result is larger than the original GIF, which was already well"
              " compressed. Keep the original, or try --preset small.")
    return out


MENU = [
    ("standard", "Standard"),
    ("high", "High quality / larger size"),
    ("small", "High compression / smaller size"),
    ("smallest", "Smallest  (high compression + max 30fps)"),
]
TIME_RE = re.compile(r"^(\d+:){0,2}\d+(\.\d+)?$")  # 12.5 / 1:05 / 0:01:05.5


def ask(prompt: str, valid) -> str:
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return ""
        if valid(answer):
            return answer
        print("    not understood, try again")


def interactive(args) -> None:
    """Prompt for preset and trim (used by the drag-and-drop .bat)."""
    print("Choose a preset:")
    for i, (_, label) in enumerate(MENU, 1):
        print(f"  {i}) {label}")
    choice = ask(f"Preset [1-{len(MENU)}, Enter = 1]: ",
                 lambda a: a == "" or (a.isdigit() and 1 <= int(a) <= len(MENU)))
    args.preset = MENU[int(choice or 1) - 1][0]

    print()
    print("Trim (press Enter to keep the whole clip). Times: seconds like 12.5, or 1:05")
    if len(args.inputs) == 1 and args.inputs[0].is_file():
        try:
            print(f"  Clip length: {probe(args.inputs[0])['duration']:.1f}s")
        except Exception:
            pass
    else:
        print("  Note: the same trim is applied to every file.")
    is_time = lambda a: a == "" or bool(TIME_RE.match(a))
    args.start = ask("  Start at: ", is_time) or None
    args.duration = ask("  Length:   ", is_time) or None
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="Make TeamSpeak avatar GIFs from video clips or GIFs.")
    p.add_argument("inputs", nargs="+", type=Path, help="video or GIF file(s)")
    p.add_argument("-o", "--output", help="output path (single input only; default: next to source)")
    p.add_argument("-p", "--preset", choices=PRESETS, default="standard",
                   help="high (256 colours + light dither), standard (256, default),"
                        " small (128 + light lossy), smallest (small, max 30fps)")
    p.add_argument("--colors", type=int, help="palette size 2-256; overrides the preset")
    p.add_argument("--dither", choices=DITHERS, help="dithering method; overrides the preset")
    p.add_argument("--lossy", type=int,
                   help="gifsicle lossy level (0 = off); overrides the preset")
    p.add_argument("-s", "--size", type=int, default=300, help="max width/height in px (default 300)")
    p.add_argument("--fps", type=float,
                   help=f"override frame rate (default: match source, capped at {MAX_GIF_FPS},"
                        " or 30 for the smallest preset)")
    p.add_argument("--force60", action="store_true",
                   help="non-standard: allow up to 60fps using mixed 1/100s and 2/100s frame"
                        " delays. Many viewers slow 1/100s frames down, so it may stutter")
    p.add_argument("--square", action="store_true", help="center-crop to a square before scaling")
    p.add_argument("--start", help="start time, e.g. 3.5 or 00:00:03.5")
    p.add_argument("--end", help="end time (in source timeline)")
    p.add_argument("--duration", help="length to keep instead of --end")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="prompt for preset and trim (used by Make GIF.bat)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.interactive:
        interactive(args)

    if args.output and len(args.inputs) > 1:
        p.error("--output can only be used with a single input")
    colors, dither, lossy, _ = PRESETS[args.preset]
    args.colors = args.colors or colors
    args.dither = args.dither or dither
    args.lossy = lossy if args.lossy is None else args.lossy
    if not 2 <= args.colors <= 256:
        p.error("--colors must be 2-256")
    fps_limit = FORCE60_FPS if args.force60 else MAX_GIF_FPS
    if args.fps is not None and not 0 < args.fps <= fps_limit:
        p.error(f"--fps must be between 0 and {fps_limit}"
                + ("" if args.force60 else " (up to 60 with --force60)"))
    missing = [n for n, exe in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE),
                                ("gifsicle", GIFSICLE)) if not exe]
    if missing:
        p.error(f"can't find {', '.join(missing)}. Put them in {BIN} or on your PATH"
                " (see README).")

    failed = 0
    for src in args.inputs:
        try:
            convert(src, args)
        except Exception as e:  # keep going on batch conversions
            failed += 1
            print(f"  !! {src.name} failed: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
