# TeamSpeak GIF Maker

Turn video clips (or oversized GIFs) into animated GIFs that work as **TeamSpeak avatars**: at most 300 px on any side, playing at the right speed, and as small as possible without visible quality loss.

Double-click `Make GIF.bat` (or drop files onto it) to open the interface, drag your clips onto the window, pick a preset and press **Convert**.

```text
                         TeamSpeak GIF Maker · Avatar GIF Converter

  Convert              Files (1)
                       Add         [                                                       ]
  Activity Log
                        fauna.mp4                720x954 · 30fps · 15.0s · loops every 6.0s (x2.5)
  Exit
                       Preset      (X) Standard   ( ) Small   ( ) Smallest
                                   Best quality: full 256-colour palette, no lossy steps.

                       Over 50fps  (X) Correct speed (50fps)   ( ) Smooth (all frames, slower)
                                   For 60fps clips. Smooth keeps every frame, plays at ~83% speed.
                       Trim        Start [0:00    ]  Length  [all     ] e.g. 12.5 or 1:05

                       Options     [X] Keep one loop if a clip repeats    [ ] Crop to square

                       Save to     [next to each source file                               ]

                         Convert      Clear List      Open Output Folder

 Tab/↑↓ move · Space select · ←→ change · Del remove file · Esc menu · F1 help · Ctrl+Q quit
 ● Ready · 1 file(s)                                             v1.0.0  Sun 22 Nov 2026 17:45
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

**Interface (Windows):** run `Make GIF.bat`. Add files by dropping them onto the .bat, onto the open window, or by typing a path into *Add*. Each file is checked in the background: resolution, frame rate, length, and whether it loops. Mouse and keyboard both work; press F1 for help. Your choices are remembered between sessions.

Each GIF is saved as `name_<preset>_<date>_<time>.gif` (e.g. `clip_standard_2026-11-22_174512.gif`; non-default settings are added, e.g. `clip_small-smooth-600px_...`), next to its source or in the *Save to* folder, so existing files, including the source, are never overwritten.

**Command line:**

```
python make_gif.py clip.mp4                        # standard preset
python make_gif.py clip.mp4 -p small               # pick a preset
python make_gif.py clip.mp4 --start 5 --duration 10
python make_gif.py clip.mp4 --square               # centre-crop to a square first
python make_gif.py *.mp4                           # batch
python make_gif.py clip.mp4 --output-dir D:\gifs     # save somewhere else
python tui.py clip.mp4                             # the interface, from a console
python make_gif.py --help                          # all options
```

| Option | Meaning |
|---|---|
| `-p, --preset` | `standard` (default), `small`, `smallest` |
| `--start`, `--end`, `--duration` | trim, in seconds (`12.5`) or `m:ss` (`1:05`) |
| `--fps N` | force a frame rate (default: match the source, max 50) |
| `--faux60` | for sources over 50 fps: keep every frame for perfectly smooth motion, playing slower (60 fps source -> 83% speed) |
| `--force60` | non-standard: allow up to 60 fps using mixed 1/100 s and 2/100 s frame delays (see below) |
| `-s, --size N` | max width/height in px (default 300, TeamSpeak's limit; larger sizes are for use elsewhere and print a warning) |
| `--loop` | if the clip repeats (e.g. an idle animation played 2.5 times), keep a single loop. The drag-and-drop flow detects this and asks |
| `--square` | centre-crop to a square before scaling |
| `--colors`, `--dither`, `--lossy` | override the preset's palette size, dithering and gifsicle lossy level |
| `-o, --output` | output path (single input only) |
| `--output-dir DIR` | save the GIFs in this folder instead of next to each source |

## Presets

| Preset | Palette | Dither | Lossy | Max fps | Notes |
|---|---|---|---|---|---|
| standard | 256 | none | — | 50 | best quality |
| small | 128 | Atkinson | 30 | 50 | ~15–25% smaller; light dithering hides most banding |
| smallest | 128 | Atkinson | 30 | 30 | small + fewer frames on high-fps clips |

**Standard is the best quality a TeamSpeak GIF can have**: the full 256-colour palette, no dithering, and no lossy steps. A bigger file wouldn't look better. Dithering mixes neighbouring colours to fake shades the palette lacks: it hides banding but adds fine grain, so it only pays off at 128 colours, where banding is visible. `--dither atkinson` adds it to Standard for the odd clip with banded gradients.

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
