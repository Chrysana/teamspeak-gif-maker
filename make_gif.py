# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert video clips (or GIFs) into GIFs sized for TeamSpeak avatars.

Pipeline: ffmpeg (trim, match fps, lanczos downscale, one shared colour
palette for the whole clip) -> gifsicle -O3 (store only changed pixels).

TeamSpeak 6 rejects GIFs with per-frame ("local") colour tables with an
"invalid size" error, so every frame must share the single global palette.
That rules out gifski, whose output always uses per-frame palettes.

Usage:
    python make_gif.py clip.mp4 [more clips...] [options]
    python tui.py [files...]          (interactive interface, used by "Make GIF.bat")
"""

import argparse
import json
import operator
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from fractions import Fraction
from pathlib import Path

__version__ = "1.0.0"

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
# --faux60: the opposite trade-off. Keep every frame of a >50fps source but give
# each a plain 2/100s delay, so motion is perfectly even and TeamSpeak plays it
# smoothly, at the cost of slower playback (60fps source -> 83% speed).

DITHERS = {"none": "none", "atkinson": "atkinson", "floyd": "floyd_steinberg",
           "sierra": "sierra2_4a", "bayer": "bayer:bayer_scale=4"}

# preset -> (palette colours, dither, gifsicle lossy level, max fps). Measured on test clips.
# Standard is the best quality a TeamSpeak GIF can have: the full 256-colour palette and no
# lossy steps. At 128 colours, banding shows in gradients; Atkinson dithering (it spreads
# only 3/4 of the colour error, so it adds little grain) hides most of it for +1-8% size.
PRESETS = {
    "standard": (256, "none", 0, MAX_GIF_FPS),      # best quality
    "small":    (128, "atkinson", 30, MAX_GIF_FPS),  # ~15-25% smaller
    "smallest": (128, "atkinson", 30, 30),           # small + max 30fps
    # Old "high" preset (standard + light dither), kept so existing commands still work.
    "high":     (256, "atkinson", 0, MAX_GIF_FPS),
}
MAIN_PRESETS = ("standard", "small", "smallest")

TIME_RE = re.compile(r"^(\d+:){0,2}\d+(\.\d+)?$")  # 12.5 / 1:05 / 0:01:05.5


class Cancelled(Exception):
    """Raised when a conversion is cancelled through its cancel event."""


def missing_tools() -> list[str]:
    return [n for n, exe in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE), ("gifsicle", GIFSICLE))
            if not exe]


def parse_time(text: str) -> float:
    """'12.5', '1:05' or '0:01:05.5' -> seconds."""
    seconds = 0.0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def format_size(n: int) -> str:
    return f"{n / 1048576:.2f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def _tool_error(cmd: list, stderr: bytes, code: int) -> RuntimeError:
    lines = stderr.decode("utf-8", "replace").strip().splitlines()
    return RuntimeError(f"{Path(cmd[0]).stem} failed: " + (lines[-1] if lines else f"exit code {code}"))


def run_capture(cmd: list) -> bytes:
    """Run a tool and return its stdout. stdin is never shared with the console."""
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)
    if proc.returncode:
        raise _tool_error(cmd, proc.stderr, proc.returncode)
    return proc.stdout


def run_tool(cmd: list, cancel=None) -> None:
    """Run a tool quietly (errors are raised, not printed), optionally cancellable."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err)
        while True:
            try:
                code = proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
        if code:
            err.seek(0)
            raise _tool_error(cmd, err.read(), code)


def probe(path: Path) -> dict:
    out = run_capture(
        [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
         "-count_packets",
         "-show_entries",
         "stream=width,height,avg_frame_rate,r_frame_rate,nb_read_packets:format=duration,format_name",
         "-of", "json", str(path)])
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


def build_filter(fps: float, size: int, square: bool, colors: int, dither: str,
                 slowdown: float = 1.0) -> str:
    parts = []
    if slowdown != 1.0:
        # Stretch timestamps so every source frame lands on its own output frame.
        parts.append(f"setpts=PTS*{slowdown:.6f}")
    parts.append(f"fps={fps:g}")
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


TEAMSPEAK_MAX_SIZE = 300


def output_label(args) -> str:
    """Settings part of the file name: the preset, plus anything non-default."""
    label = args.preset
    if args.faux60:
        label += "-smooth"
    if args.force60:
        label += "-force60"
    if args.size != TEAMSPEAK_MAX_SIZE:
        label += f"-{args.size}px"
    return label


def default_output(src: Path, folder: Path | None = None, label: str = "") -> Path:
    """<name>_<label>_<date>_<time>.gif next to the source (or in folder); never an existing file."""
    folder = folder or src.parent
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = f"{src.stem}_{label}_{stamp}" if label else f"{src.stem}_{stamp}"
    out = folder / f"{base}.gif"
    n = 2
    while out.exists():
        out = folder / f"{base}-{n}.gif"
        n += 1
    return out


def detect_loop(src: Path, info: dict, start=None, end=None, duration=None) -> dict | None:
    """Find the shortest cycle the clip repeats (e.g. an idle animation played 2.5x).

    Compares tiny greyscale thumbnails: offset P is a loop if the first ~0.5s of
    frames match the frames P later about as closely as consecutive frames match
    each other, so cutting the clip to P frames leaves an invisible seam.
    """
    w = 64
    h = max(2, round(w * info["height"] / info["width"]))
    cmd = [str(FFMPEG), "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", start]
    if end:
        cmd += ["-to", end]
    if duration:
        cmd += ["-t", duration]
    cmd += ["-i", str(src), "-an", "-vf", f"scale={w}:{h}:flags=area,format=gray",
            "-fps_mode", "passthrough", "-frames:v", "3000", "-f", "rawvideo", "-"]
    data = run_capture(cmd)
    size = w * h
    frames = [data[i:i + size] for i in range(0, len(data) - size + 1, size)]
    n = len(frames)
    fps = info["fps"]
    if n < 20:
        return None

    def diff(a: bytes, b: bytes) -> float:
        return sum(map(abs, map(operator.sub, a, b))) / size

    def peak(a: bytes, b: bytes) -> int:
        return max(map(abs, map(operator.sub, a, b)))

    steps = sorted(diff(frames[i], frames[i + 1]) for i in range(n - 1))
    step = steps[len(steps) // 2]                 # typical frame-to-frame change
    peaks = sorted(peak(frames[i], frames[i + 1]) for i in range(n - 1))
    peak_limit = 3 * peaks[int(len(peaks) * 0.95)] + 16  # well above normal local change
    window = max(3, min(round(fps / 2), n // 4))  # compare ~0.5s of frames
    min_period = max(2, round(fps / 2))           # ignore cycles under ~0.5s
    scores = {
        period: sum(diff(frames[i], frames[i + period]) for i in range(window)) / window
        for period in range(min_period, n - window + 1)
    }
    if not scores:
        return None
    typical = sorted(scores.values())[len(scores) // 2]
    limit = max(1.5 * step, 0.5)
    candidates = [q for q in sorted(scores) if scores[q] <= limit and scores[q] <= typical / 2]
    for period in candidates:
        while period + 1 in scores and scores[period + 1] < scores[period]:
            period += 1  # settle on the best-matching frame
        if n < period * 1.2:
            break  # less than ~one extra cycle: nothing worth trimming
        # The whole clip must repeat, not just its first half second; otherwise a
        # mostly-still clip with one event (a blink) could be cut short.
        # A local event (a blink) barely moves the average, so also check the
        # largest single-pixel change against normal frame-to-frame changes.
        overlap = [diff(frames[i], frames[i + period]) for i in range(n - period)]
        worst = max(peak(frames[i], frames[i + period]) for i in range(n - period))
        if sum(overlap) / len(overlap) <= limit and worst <= peak_limit:
            return {"frames": period, "seconds": period / fps, "cycles": n / period}
    return None


def options(**overrides) -> argparse.Namespace:
    """Conversion options with defaults; colours/dither/lossy follow the preset unless given."""
    opts = argparse.Namespace(
        preset="standard", colors=None, dither=None, lossy=None, size=300, fps=None,
        force60=False, faux60=False, loop=False, square=False,
        start=None, end=None, duration=None, output=None, output_dir=None)
    for key, value in overrides.items():
        setattr(opts, key, value)
    return resolve_preset(opts)


def resolve_preset(opts: argparse.Namespace) -> argparse.Namespace:
    colors, dither, lossy, _ = PRESETS[opts.preset]
    opts.colors = opts.colors or colors
    opts.dither = opts.dither or dither
    opts.lossy = lossy if opts.lossy is None else opts.lossy
    return opts


def convert(src: Path, args, log=print, cancel=None, stage=None, known_loop=...) -> dict:
    """Convert one file. Messages go to log(); stage(text) reports progress steps.

    known_loop: a detect_loop() result for the untrimmed clip, if already computed.
    Returns {"path", "width", "height", "duration", "bytes", "warnings"}.
    """
    stage = stage or (lambda text: None)

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    if not src.is_file():
        raise FileNotFoundError("file not found")
    stage("Reading")
    info = probe(src)
    cap = PRESETS[args.preset][3]
    if args.force60 and cap == MAX_GIF_FPS:
        cap = FORCE60_FPS
    fps = args.fps or min(info["fps"], cap)
    slowdown = 1.0
    if args.faux60:
        if info["fps"] > MAX_GIF_FPS:
            slowdown = info["fps"] / MAX_GIF_FPS
            fps = MAX_GIF_FPS
        else:
            log(f"  note: source is {info['fps']:g}fps, so faux 60 has no effect")
    if args.output:
        out = Path(args.output)
    else:
        out = default_output(src, Path(args.output_dir) if args.output_dir else None,
                             output_label(args))
    if out.resolve() == src.resolve():
        raise ValueError("output path is the input file; choose a different output")

    end, duration = args.end, args.duration
    if args.loop:
        trimmed = args.start or args.end or args.duration
        if known_loop is not ... and not trimmed:
            lp = known_loop
        else:
            stage("Finding loop")
            lp = detect_loop(src, info, args.start, args.end, args.duration)
        check_cancel()
        if lp:
            # keep frames [0, period): the next frame would be the start again
            duration = f"{(lp['frames'] - 0.5) / info['fps']:.4f}"
            end = None
            log(f"  loop: {lp['seconds']:.2f}s cycle repeats {lp['cycles']:.1f}x"
                " -> keeping one loop")
        else:
            log("  loop: no repeating cycle found, keeping the whole clip")

    log(f"{src.name}: {info['width']}x{info['height']} @ {info['fps']:g}fps"
        f" -> GIF @ {fps:g}fps, {args.colors} colours, dither {args.dither}"
        + (f", lossy {args.lossy}" if args.lossy else "")
        + (f", all frames kept, plays at {100 / slowdown:.0f}% speed" if slowdown != 1 else ""))
    if args.size > TEAMSPEAK_MAX_SIZE:
        log(f"  note: --size {args.size} is above TeamSpeak's {TEAMSPEAK_MAX_SIZE}px avatar limit;"
            " TeamSpeak will reject this GIF as an avatar")
    if fps > MAX_GIF_FPS:
        log("  note: --force60 uses 1/100s frame delays, which many viewers slow down"
            " (TeamSpeak 6 plays them very slowly)")

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.gif"
        ff = [str(FFMPEG), "-nostdin", "-v", "error", "-y",
              # high-quality scaling for automatic pixel-format conversions too
              "-sws_flags", "lanczos+accurate_rnd+full_chroma_int"]
        if args.start:
            ff += ["-ss", args.start]
        if end:
            ff += ["-to", end]
        if duration:
            ff += ["-t", duration]
        ff += ["-i", str(src)]
        ff += ["-an", "-lavfi",
               build_filter(fps, args.size, args.square, args.colors, args.dither, slowdown),
               "-loop", "0", str(raw)]
        check_cancel()
        stage("Encoding")
        run_tool(ff, cancel)
        gs = [str(GIFSICLE), "--no-warnings", "-O3"]
        if args.lossy:
            gs.append(f"--lossy={args.lossy}")
        stage("Optimizing")
        run_tool(gs + [str(raw), "-o", str(out)], cancel)

    res = probe(out)
    size = out.stat().st_size
    log(f"  -> {out}  {res['width']}x{res['height']}, {res['duration']:.2f}s, {format_size(size)}")
    warnings = []
    if local_palette_count(out):
        warnings.append("output has per-frame colour tables; TeamSpeak will reject it")
    if info["is_gif"] and size > src.stat().st_size:
        warnings.append("result is larger than the original GIF, which was already well"
                        " compressed. Keep the original, or try a smaller preset.")
    for w in warnings:
        log(f"  note: {w}")
    return {"path": out, "width": res["width"], "height": res["height"],
            "duration": res["duration"], "bytes": size, "warnings": warnings}


MENU = [  # (preset, faux60, label)
    ("standard", False, "Standard  (best quality)"),
    ("small", False, "High compression / smaller size"),
    ("smallest", False, "Smallest  (high compression + max 30fps)"),
    ("standard", True, "Smoothest motion  (60fps clips: keep every frame, plays ~83% speed)"),
]


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
    """Simple prompts for preset and trim (the .bat now opens the full interface, tui.py)."""
    print("Choose a preset:")
    for i, (_, _, label) in enumerate(MENU, 1):
        print(f"  {i}) {label}")
    choice = ask(f"Preset [1-{len(MENU)}, Enter = 1]: ",
                 lambda a: a == "" or (a.isdigit() and 1 <= int(a) <= len(MENU)))
    args.preset, faux60, _ = MENU[int(choice or 1) - 1]
    args.faux60 = args.faux60 or faux60

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

    yes_no = lambda a: a.lower() in ("", "y", "yes", "n", "no")
    src = args.inputs[0]
    if len(args.inputs) == 1 and src.is_file():
        try:
            lp = detect_loop(src, probe(src), args.start, None, args.duration)
        except Exception:
            lp = None
        if lp:
            print(f"\n  This clip loops: a {lp['seconds']:.2f}s cycle repeats {lp['cycles']:.1f}x."
                  f" Keeping one loop makes the GIF ~{1 - 1 / lp['cycles']:.0%} smaller.")
            args.loop = ask("  Keep just one loop? [Y/n]: ", yes_no).lower() in ("", "y", "yes")
    elif len(args.inputs) > 1:
        print()
        args.loop = ask("  Trim clips that repeat to a single loop? [y/N]: ",
                        yes_no).lower() in ("y", "yes")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="Make TeamSpeak avatar GIFs from video clips or GIFs.")
    p.add_argument("inputs", nargs="+", type=Path, help="video or GIF file(s)")
    p.add_argument("-o", "--output", help="output path (single input only; default: <name>_<date>_<time>.gif"
                        " next to the source)")
    p.add_argument("--output-dir", help="folder for the output GIFs (default: next to each source)")
    p.add_argument("-p", "--preset", choices=PRESETS, default="standard",
                   metavar="{" + ",".join(MAIN_PRESETS) + "}",
                   help="standard (best quality, default), small (128 colours + light dither"
                        " and lossy), smallest (small, max 30fps)")
    p.add_argument("--colors", type=int, help="palette size 2-256; overrides the preset")
    p.add_argument("--dither", choices=DITHERS, help="dithering method; overrides the preset")
    p.add_argument("--lossy", type=int,
                   help="gifsicle lossy level (0 = off); overrides the preset")
    p.add_argument("-s", "--size", type=int, default=TEAMSPEAK_MAX_SIZE,
                   help="max width/height in px (default 300, TeamSpeak's avatar limit; larger"
                        " sizes are for use elsewhere)")
    p.add_argument("--fps", type=float,
                   help=f"override frame rate (default: match source, capped at {MAX_GIF_FPS},"
                        " or 30 for the smallest preset)")
    p.add_argument("--force60", action="store_true",
                   help="non-standard: allow up to 60fps using mixed 1/100s and 2/100s frame"
                        " delays. Many viewers slow 1/100s frames down; TeamSpeak 6 plays"
                        " these GIFs very slowly")
    p.add_argument("--faux60", action="store_true",
                   help="for sources over 50fps: keep every frame for perfectly smooth motion,"
                        " playing slower (60fps source -> 83%% speed)")
    p.add_argument("--loop", action="store_true",
                   help="if the clip repeats (e.g. an idle animation), keep a single loop")
    p.add_argument("--square", action="store_true", help="center-crop to a square before scaling")
    p.add_argument("--start", help="start time, e.g. 3.5 or 00:00:03.5")
    p.add_argument("--end", help="end time (in source timeline)")
    p.add_argument("--duration", help="length to keep instead of --end")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="simple text prompts for preset and trim")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.interactive:
        interactive(args)

    if args.output and len(args.inputs) > 1:
        p.error("--output can only be used with a single input")
    resolve_preset(args)
    if not 2 <= args.colors <= 256:
        p.error("--colors must be 2-256")
    if args.size < 16:
        p.error("--size must be at least 16")
    if args.faux60 and (args.force60 or args.fps):
        p.error("--faux60 can't be combined with --force60 or --fps")
    fps_limit = FORCE60_FPS if args.force60 else MAX_GIF_FPS
    if args.fps is not None and not 0 < args.fps <= fps_limit:
        p.error(f"--fps must be between 0 and {fps_limit}"
                + ("" if args.force60 else " (up to 60 with --force60)"))
    if args.output_dir and not Path(args.output_dir).is_dir():
        p.error(f"--output-dir {args.output_dir} is not a folder")
    if missing_tools():
        p.error(f"can't find {', '.join(missing_tools())}. Put them in {BIN} or on your PATH"
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
