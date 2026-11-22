# TeamSpeak GIF Maker

Turn video clips (or oversized GIFs) into animated GIFs that work as **TeamSpeak avatars**: at most 300 px on any side, playing at the right speed, and as small as possible without visible quality loss.

Drag files onto `Make GIF.bat`, pick a preset, optionally trim, done.

```
Choose a preset:
  1) Standard
  2) High quality / larger size
  3) High compression / smaller size
  4) Smallest  (high compression + max 30fps)
Preset [1-4, Enter = 1]: 1

Trim (press Enter to keep the whole clip). Times: seconds like 12.5, or 1:05
  Clip length: 23.9s
  Start at: 0:05
  Length:   10

clip.mp4: 1280x720 @ 60fps -> GIF @ 50fps, 256 colours, dither none
  -> clip.gif  300x169, 10.00s, 11.86 MB
```

## Requirements

- **Python 3.10+**
- **ffmpeg** and **ffprobe**: [ffmpeg.org/download](https://ffmpeg.org/download.html) (Windows builds: [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or [BtbN](https://github.com/BtbN/FFmpeg-Builds/releases)), or `winget install Gyan.FFmpeg`
- **gifsicle**: [lcdf.org/gifsicle](https://www.lcdf.org/gifsicle/) (Windows builds: [eternallybored.org](https://eternallybored.org/misc/gifsicle/))

The script looks for the tools in this order:

1. `bin/ffmpeg/ffmpeg.exe`, `bin/ffmpeg/ffprobe.exe`, `bin/gifsicle/gifsicle.exe`
2. directly in `bin/`
3. your `PATH`

## Usage

**Drag and drop (Windows):** drop one or more videos or GIFs onto `Make GIF.bat`. Each GIF is saved next to its source. GIF inputs are saved as `name_ts.gif`, so the original is never overwritten.

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
| `-s, --size N` | max width/height in px (default 300) |
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

1. **Clip length.** Size scales almost linearly with duration. Trim to the part that loops.
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
- **Scaling happens before colour reduction.** Resizing an already-quantised GIF re-dithers it and loses quality.
- **Heavy `--lossy` is avoided.** gifsicle's lossy mode at high levels causes obvious noise and streaks; only a light level is used, and only in the small presets.

## Tool versions

[`.github/tool-versions.json`](.github/tool-versions.json) records the ffmpeg and gifsicle versions this project was last tested with. A weekly workflow compares them with upstream releases and opens a [`tool-update`](../../issues?q=label%3Atool-update) issue when a newer one is out.

## License

[GPL-3.0-or-later](LICENSE). ffmpeg and gifsicle are separate projects under their own licenses and are not included in this repository.
