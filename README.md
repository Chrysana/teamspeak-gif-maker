# TeamSpeak GIF Maker

Turn video clips (or oversized GIFs) into animated GIFs that work as **TeamSpeak avatars**: at most 300 px on any side, playing at the right speed, and as small as possible without visible quality loss.

Drag files onto `Make GIF.bat`, pick a preset, optionally trim, done.

```
Choose a preset:
  1) Standard
  2) High quality / larger size
  3) High compression / smaller size
  4) Smallest  (high compression + max 30fps)
  5) Smoothest motion  (60fps clips: keep every frame, plays ~83% speed)
Preset [1-5, Enter = 1]: 1

Trim (press Enter to keep the whole clip). Times: seconds like 12.5, or 1:05
  Clip length: 23.9s
  Start at: 0:05
  Length:   10

clip.mp4: 1280x720 @ 60fps -> GIF @ 50fps, 256 colours, dither none
  -> clip.gif  300x169, 10.00s, 11.86 MB
```

## Download (Windows)

Grab `teamspeak-gif-maker-<version>-win64.zip` from the [latest release](../../releases/latest), extract it anywhere, and drag a clip onto `Make GIF.bat`. Python, ffmpeg and gifsicle are all included; nothing else to install.

## Running from source

Requirements:

- **Python 3.10+**
- **ffmpeg** and **ffprobe**: [ffmpeg.org/download](https://ffmpeg.org/download.html) (Windows builds: [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or [BtbN](https://github.com/BtbN/FFmpeg-Builds/releases)), or `winget install Gyan.FFmpeg`
- **gifsicle**: [lcdf.org/gifsicle](https://www.lcdf.org/gifsicle/) (Windows builds: [eternallybored.org](https://eternallybored.org/misc/gifsicle/))

The script looks for the tools in this order:

1. `bin/ffmpeg/ffmpeg.exe`, `bin/ffmpeg/ffprobe.exe`, `bin/gifsicle/gifsicle.exe`
2. directly in `bin/`
3. your `PATH`

## Usage

**Drag and drop (Windows):** drop one or more videos or GIFs onto `Make GIF.bat`. Each GIF is saved next to its source as `name_<date>_<time>.gif` (e.g. `clip_2026-11-22_174512.gif`), so existing files, including the source, are never overwritten.

**Command line:**

```
python make_gif.py clip.mp4                        # standard preset
python make_gif.py clip.mp4 -p small               # pick a preset
python make_gif.py clip.mp4 --start 5 --duration 10
python make_gif.py clip.mp4 --square               # centre-crop to a square first
python make_gif.py *.mp4                           # batch
python make_gif.py --help                          # all options
```

| Option | Meaning |
|---|---|
| `-p, --preset` | `standard` (default), `high`, `small`, `smallest` |
| `--start`, `--end`, `--duration` | trim, in seconds (`12.5`) or `m:ss` (`1:05`) |
| `--fps N` | force a frame rate (default: match the source, max 50) |
| `--faux60` | for sources over 50 fps: keep every frame for perfectly smooth motion, playing slower (60 fps source -> 83% speed) |
| `--force60` | non-standard: allow up to 60 fps using mixed 1/100 s and 2/100 s frame delays (see below) |
| `-s, --size N` | max width/height in px (default 300) |
| `--loop` | if the clip repeats (e.g. an idle animation played 2.5 times), keep a single loop. The drag-and-drop flow detects this and asks |
| `--square` | centre-crop to a square before scaling |
| `--colors`, `--dither`, `--lossy` | override the preset's palette size, dithering and gifsicle lossy level |
| `-o, --output` | output path (single input only) |

## Presets

| Preset | Palette | Dither | Lossy | Max fps | Notes |
|---|---|---|---|---|---|
| standard | 256 | none | — | 50 | most accurate per byte |
| high | 256 | Atkinson | — | 50 | softens gradient banding, same size as standard |
| small | 128 | none | 30 | 50 | ~20–35% smaller, some banding in gradients |
| smallest | 128 | none | 30 | 30 | small + fewer frames on high-fps clips |

**What actually controls file size**, from most to least effective:

1. **Clip length.** Size scales almost linearly with duration. Trim to the part that loops; for clips that repeat, `--loop` (or the drag-and-drop prompt) finds the cycle automatically.
2. **Frame rate.** A 60 fps clip at 30 fps is roughly a third smaller.
3. **Content.** Camera movement, noise and grain are expensive; still backgrounds are almost free.
4. **Preset.**

Expect the GIF to be several times bigger than the source MP4. Modern video codecs are far more efficient than GIF, and downscaling doesn't close that gap.

## How it works

```
ffmpeg:   trim -> resample to source fps -> lanczos downscale -> one palette for the whole clip
gifsicle: -O3 (store only the pixels that change between frames) [+ --lossy for small presets]
```

Design notes, all based on testing against real TeamSpeak 6 clients:

- **TeamSpeak 6 rejects GIFs with per-frame colour tables** and reports "invalid size". Every frame must use the single global palette. This rules out [gifski](https://gif.ski/), which otherwise produces excellent GIFs but always writes per-frame palettes. The script checks its output and warns if any per-frame tables appear.
- **Playback speed matches the source.** GIF frame delays are stored in 1/100 s, so 50 fps is the fastest rate that can be represented exactly. Faster sources are resampled to 50 fps rather than kept frame-for-frame, which would make the GIF play slower than the source.
- **`--faux60` trades speed for smoothness.** It keeps every frame of a 60 fps clip, each with a plain 2/100 s delay, so motion is perfectly even and TeamSpeak plays it correctly, but the clip runs at 83% speed (20% longer). It's also in the drag-and-drop menu as option 5.
- **`--force60` is opt-in.** 60 fps can be averaged by alternating 2/100 s and 1/100 s delays. The timing is correct, but browsers and many viewers slow 1/100 s frames down to about 10/100 s, so it can stutter. **TeamSpeak 6 plays these GIFs very slowly**, so don't use it for avatars; it's kept for other viewers that honour short delays.
- **Loop detection** compares small thumbnails of every frame to find the shortest cycle the clip repeats, and only accepts it if the cut-off point blends back into the start about as smoothly as two normal consecutive frames. If a clip repeats only approximately, a brief moment that happens in a later repeat only (e.g. a single blink) may be lost, so check the result.
- **Scaling happens before colour reduction.** Resizing an already-quantised GIF re-dithers it and loses quality.
- **Heavy `--lossy` is avoided.** gifsicle's lossy mode at high levels causes obvious noise and streaks; only a light level is used, and only in the small presets.

## Tool versions

[`.github/tool-versions.json`](.github/tool-versions.json) records the ffmpeg and gifsicle versions this project was last tested with. A weekly workflow compares them with upstream releases and opens a [`tool-update`](../../issues?q=label%3Atool-update) issue when a newer one is out.

## License

This project is licensed under [GPL-3.0-or-later](LICENSE).

ffmpeg and gifsicle are separate programs that the script runs from the command line. They are not part of this repository and keep their own licenses:

- **ffmpeg**: [LGPL-2.1-or-later](https://github.com/FFmpeg/FFmpeg/blob/master/LICENSE.md), or GPL depending on how a build was configured. The release zip bundles an LGPL build.
- **gifsicle**: [GPL-2.0-only](https://github.com/kohler/gifsicle/blob/master/COPYING), © Eddie Kohler.
- **Python** (release zip only): [PSF License](https://docs.python.org/3/license.html).

The Windows release zip includes each program's license text and a `THIRD-PARTY-NOTICES.md` with exact versions. The matching ffmpeg and gifsicle source code is attached to every release. The zip is assembled by [`.github/workflows/release.yml`](.github/workflows/release.yml). If you redistribute these binaries yourself, you must follow their licenses too.

Not affiliated with or endorsed by TeamSpeak Systems GmbH. TeamSpeak is a trademark of its respective owner.
