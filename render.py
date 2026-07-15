#!/usr/bin/env python3
"""space-demo-kit prototype renderer.

Usage: python3 render.py manifests/<name>.json

Renders a manifest through templates/app-window.html with Playwright by
stepping a deterministic window.seek(t) timeline at 30 fps, then assembles
the frames with ffmpeg (H.264, bt709, faststart). For audio-bearing modes
(avatar, a2a) the source audio is muxed in, delayed to the timeline's
playStart so it lines up with the on-screen playback. Emits a 2x2 contact
sheet next to the MP4 under out/<name>/.
"""

import array
import json
import math
import subprocess
import sys
import urllib.request
from pathlib import Path

KIT = Path(__file__).resolve().parent
FPS = 30
WIDTH, HEIGHT = 1440, 1080

FONTS = {
    "source-sans-3-400.woff2": "https://cdn.jsdelivr.net/fontsource/fonts/source-sans-3@latest/latin-400-normal.woff2",
    "source-sans-3-600.woff2": "https://cdn.jsdelivr.net/fontsource/fonts/source-sans-3@latest/latin-600-normal.woff2",
    "source-sans-3-700.woff2": "https://cdn.jsdelivr.net/fontsource/fonts/source-sans-3@latest/latin-700-normal.woff2",
    "ibm-plex-mono-400.woff2": "https://cdn.jsdelivr.net/fontsource/fonts/ibm-plex-mono@latest/latin-400-normal.woff2",
    "ibm-plex-mono-500.woff2": "https://cdn.jsdelivr.net/fontsource/fonts/ibm-plex-mono@latest/latin-500-normal.woff2",
    "space-grotesk-500.woff2": "https://cdn.jsdelivr.net/fontsource/fonts/space-grotesk@latest/latin-500-normal.woff2",
    "space-grotesk-700.woff2": "https://cdn.jsdelivr.net/fontsource/fonts/space-grotesk@latest/latin-700-normal.woff2",
}
TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/svg/"
HF_LOGO_URL = "https://huggingface.co/front/assets/huggingface_logo-noborder.svg"


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as r:
        dest.write_bytes(r.read())
    print(f"fetched {dest.name}")


def ensure_fonts() -> None:
    for name, url in FONTS.items():
        fetch(url, KIT / "fonts" / name)


def emoji_asset(emoji: str) -> Path:
    codes = "-".join(f"{ord(c):x}" for c in emoji if ord(c) != 0xFE0F)
    dest = KIT / "assets" / "emoji" / f"{codes}.svg"
    fetch(TWEMOJI_BASE + f"{codes}.svg", dest)
    return dest


def hf_logo() -> Path:
    dest = KIT / "assets" / "hf-logo.svg"
    fetch(HF_LOGO_URL, dest)
    return dest


def extract_frames(video: Path, outdir: Path) -> tuple[int, float]:
    """Extract source video frames at native fps; return (count, fps)."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    num, den = probe.split("/")
    fps = float(num) / float(den)
    outdir.mkdir(parents=True, exist_ok=True)
    if not any(outdir.iterdir()):
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", str(video),
             "-qscale:v", "2", str(outdir / "%05d.jpg")],
            check=True,
        )
    frames = sorted(outdir.glob("*.jpg"))
    return len(frames), fps


def image_aspect(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    return int(out[0]) / int(out[1])


def video_poster(video: Path, dest: Path, at_fraction: float = 0.15) -> None:
    """Grab one representative frame from a video as a poster thumbnail."""
    if dest.exists():
        return
    dur = media_duration(video)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{dur * at_fraction:.3f}",
         "-i", str(video), "-frames:v", "1", "-qscale:v", "2", str(dest)],
        check=True,
    )


def media_duration(path: Path) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip())


def audio_peaks(path: Path, n: int = 140) -> list[float]:
    """Downmix to mono s16 and take normalized max-abs peaks in n buckets."""
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
        capture_output=True, check=True,
    ).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if not samples:
        return [0.0] * n
    bucket = max(1, len(samples) // n)
    peaks = []
    for i in range(n):
        seg = samples[i * bucket:(i + 1) * bucket]
        peaks.append(max(abs(s) for s in seg) / 32768 if seg else 0.0)
    mx = max(peaks) or 1.0
    return [round(p / mx, 3) for p in peaks]


def main() -> None:
    manifest_path = Path(sys.argv[1]).resolve()
    m = json.loads(manifest_path.read_text())
    name = m["name"]
    template = m["template"]
    workdir = KIT / "out" / name
    shots = workdir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    for old in shots.glob("*.jpg"):
        old.unlink()

    ensure_fonts()
    runtime = dict(m)
    runtime["emoji_url"] = emoji_asset(m["space"]["emoji"]).as_uri()
    runtime["hf_emoji_url"] = emoji_asset("🤗").as_uri()
    runtime["hf_logo_url"] = hf_logo().as_uri()
    if m.get("input_image"):
        runtime["input_image"] = Path(m["input_image"]).resolve().as_uri()

    audio_play: Path | None = None  # muxed into the final mp4 at playStart

    if template in ("i2v", "t2v", "avatar"):
        count, src_fps = extract_frames(Path(m["video"]), workdir / "src_frames")
        runtime["frame_files"] = [p.as_uri() for p in sorted((workdir / "src_frames").glob("*.jpg"))]
        runtime["frame_count"] = count
        runtime["video_fps"] = src_fps
        if template == "avatar":
            audio_play = Path(m["audio"])
            runtime["peaks_in"] = audio_peaks(audio_play)
            runtime["audio_duration"] = media_duration(audio_play)
    elif template == "edit":
        runtime["before_image"] = Path(m["before_image"]).resolve().as_uri()
        runtime["after_image"] = Path(m["after_image"]).resolve().as_uri()
        runtime["after_aspect"] = image_aspect(Path(m["after_image"]))
    elif template == "a2a":
        runtime["peaks_in"] = audio_peaks(Path(m["audio_in"]))
        runtime["peaks_out"] = audio_peaks(Path(m["audio_out"]))
        runtime["audio_duration"] = media_duration(Path(m["audio_out"]))
        if m.get("audio_sequence"):
            # play the input first, then the output (ORIGINAL -> ENHANCED storytelling)
            seq = workdir / "sequence.wav"
            if not seq.exists():
                subprocess.run(
                    ["ffmpeg", "-loglevel", "error", "-y",
                     "-i", str(Path(m["audio_in"])), "-i", str(Path(m["audio_out"])),
                     "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1", str(seq)],
                    check=True,
                )
            audio_play = seq
            runtime["audio_duration_in"] = media_duration(Path(m["audio_in"]))
            runtime["audio_duration_out"] = media_duration(Path(m["audio_out"]))
        else:
            audio_play = Path(m["audio_out"])
    elif template == "t2i":
        runtime["output_image"] = Path(m["output_image"]).resolve().as_uri()
        runtime["output_aspect"] = image_aspect(Path(m["output_image"]))
    elif template == "gallery":
        runtime["outputs"] = [
            {"url": Path(o["path"]).resolve().as_uri(), "label": o["label"]}
            for o in m["outputs"]
        ]
    elif template == "vqa":
        video = Path(m["video"])
        dur = media_duration(video)
        in_dir = workdir / "input_frames"
        in_dir.mkdir(parents=True, exist_ok=True)
        if not any(in_dir.iterdir()):
            # ~180 evenly spaced frames: covers ~6s of accelerated playback at 30fps
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-i", str(video),
                 "-vf", f"fps={180 / dur:.6f}", "-qscale:v", "3", str(in_dir / "%05d.jpg")],
                check=True,
            )
        in_frames = sorted(in_dir.glob("*.jpg"))
        runtime["input_frame_files"] = [p.as_uri() for p in in_frames]
        runtime["input_image"] = in_frames[0].as_uri()
        runtime["video_duration"] = dur
    else:
        raise SystemExit(f"unknown template: {template}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--force-color-profile=srgb"])
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        style_files = {"poster": "poster.html", "fullbleed": "fullbleed.html", "cards": "cards.html"}
        tpl = style_files.get(m.get("style"), "app-window.html")
        page.goto((KIT / "templates" / tpl).as_uri())
        duration = page.evaluate("m => window.setup(m)", runtime)
        play_start = page.evaluate("() => window.__T.playStart ?? null")
        n = math.ceil(duration * FPS)
        print(f"rendering {n} frames ({duration:.2f}s @ {FPS}fps)")
        for i in range(n):
            page.evaluate("t => window.seek(t)", i / FPS)
            page.screenshot(path=str(shots / f"{i:05d}.jpg"), type="jpeg", quality=92)
            if i % 60 == 0:
                print(f"  frame {i}/{n}")
        browser.close()

    out_mp4 = workdir / f"{name}.mp4"
    total = n / FPS
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS),
         "-i", str(shots / "%05d.jpg"),
         "-c:v", "libx264", "-preset", "slow", "-crf", "17",
         "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
         "-colorspace", "bt709", "-movflags", "+faststart", str(out_mp4)],
        check=True,
    )

    if audio_play is not None and play_start is not None:
        delay_ms = int(play_start * 1000)
        silent = workdir / f"{name}-silent.mp4"
        out_mp4.rename(silent)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(silent), "-i", str(audio_play),
             "-filter_complex",
             f"[1:a]volume={m.get('audio_gain', 2.0)},alimiter=limit=0.95,"
             f"adelay={delay_ms}:all=1,apad[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}",
             "-movflags", "+faststart", str(out_mp4)],
            check=True,
        )
        silent.unlink()

    # 2x2 contact sheet for quick review / completion reports
    picks = [int(n * f) for f in (0.12, 0.38, 0.64, 0.9)]
    inputs: list[str] = []
    for p in picks:
        inputs += ["-i", str(shots / f"{p:05d}.jpg")]
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", *inputs,
         "-filter_complex", "[0][1][2][3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0,scale=1600:-1",
         str(workdir / f"{name}-contact.jpg")],
        check=True,
    )
    print(f"done: {out_mp4}")


if __name__ == "__main__":
    main()
