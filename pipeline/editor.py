"""Video editing with ffmpeg.

Steps for each video:
  1. Download any clip URLs listed in meta.yml.
  2. Normalize every clip (videos and photos) to the same resolution, fps and
     audio format. Portrait clips get a blurred background fill instead of
     black bars; photos get a slow Ken Burns zoom.
  3. Join intro + clips + outro.
  4. Final pass: retro color look, fade in/out, title text, logo watermark,
     background music that ducks under speech, and loudness normalization.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

import requests

from .config import ASSETS_DIR, IMAGE_EXTS, VideoJob

log = logging.getLogger(__name__)

RESOLUTIONS = {"landscape": (1920, 1080), "short": (1080, 1920)}

LOOKS = {
    "none": "",
    # warm faded film: vintage curves, softer colours, grain, vignette
    "vintage": "curves=preset=vintage,eq=saturation=0.85:contrast=1.05,"
               "noise=alls=10:allf=t+u,vignette=PI/5",
    # 90s camcorder: colour bleed, heavier grain, slight blur
    "vhs": "eq=saturation=1.25:contrast=1.08,rgbashift=rh=3:bh=-3,"
           "noise=alls=18:allf=t,gblur=sigma=0.6,vignette=PI/4",
}

FONT_CANDIDATES = [
    ASSETS_DIR / "fonts" / "title.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
]


def find_font() -> Path | None:
    return next((p for p in FONT_CANDIDATES if p.exists()), None)


def run(cmd: list[str]) -> None:
    log.debug("$ %s", " ".join(map(str, cmd)))
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}):\n{proc.stderr[-3000:]}")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def download(url: str, dest_dir: Path) -> Path:
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "clip.mp4"
    dest = dest_dir / f"{hashlib.sha1(url.encode()).hexdigest()[:8]}_{name}"
    if dest.exists():
        return dest
    log.info("Downloading %s", url)
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    return dest


def normalize_clip(src: Path, dest: Path, edit_cfg: dict, image_seconds: float) -> Path:
    w, h = RESOLUTIONS[edit_cfg.get("format", "landscape")]
    fps = int(edit_cfg.get("fps", 30))
    enc = ["-c:v", "libx264", "-preset", edit_cfg.get("preset", "medium"),
           "-crf", str(edit_cfg.get("crf", 20)), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

    if src.suffix.lower() in IMAGE_EXTS:
        frames = int(image_seconds * fps)
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,"
              f"crop={w * 2}:{h * 2},"
              f"zoompan=z='min(zoom+0.0007,1.12)':d={frames}"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
              "setsar=1,format=yuv420p")
        run(["ffmpeg", "-y", "-loop", "1", "-i", src,
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-vf", vf, "-t", image_seconds, *enc, dest])
        return dest

    # Blurred, zoomed copy fills the frame; the original sits on top un-cropped.
    fc = (f"[0:v]split[bgsrc][fgsrc];"
          f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
          f"boxblur=20:5[bg];"
          f"[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[v]")
    cmd = ["ffmpeg", "-y", "-i", src]
    if has_audio(src):
        audio_map = "0:a:0"
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", audio_map, "-shortest", *enc, dest]
    run(cmd)
    return dest


def pick_music(job: VideoJob) -> Path | None:
    choice = job.meta.get("music", "auto")
    if not choice or choice == "none":
        return None
    music_dir = ASSETS_DIR / "music"
    if choice != "auto":
        path = music_dir / choice
        return path if path.exists() else None
    tracks = sorted(p for p in music_dir.glob("*") if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"})
    if not tracks:
        return None
    # Picked by slug so re-running the same video gives the same track.
    idx = int(hashlib.sha1(job.slug.encode()).hexdigest(), 16) % len(tracks)
    return tracks[idx]


def edit_video(job: VideoJob, work_dir: Path) -> Path:
    """Render the final upload-ready MP4 for `job` and return its path."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    edit_cfg = job.config.get("edit", {})
    work_dir.mkdir(parents=True, exist_ok=True)
    image_seconds = float(job.meta.get("image_seconds", 4))

    sources: list[Path] = []
    intro, outro = ASSETS_DIR / "intro.mp4", ASSETS_DIR / "outro.mp4"
    if job.meta.get("intro", True) and intro.exists():
        sources.append(intro)
    for clip in job.clips:
        sources.append(download(clip, work_dir) if isinstance(clip, str) else clip)
    if job.meta.get("outro", True) and outro.exists():
        sources.append(outro)
    if not job.clips:
        raise ValueError(f"{job.slug}: no clips found (add video/photo files or a `clips:` list)")

    normalized = []
    for i, src in enumerate(sources):
        if not Path(src).exists():
            raise FileNotFoundError(src)
        log.info("[%s] normalizing %s", job.slug, Path(src).name)
        normalized.append(normalize_clip(Path(src), work_dir / f"norm_{i:03d}.mp4", edit_cfg, image_seconds))

    concat_list = work_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in normalized))
    joined = work_dir / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", joined])

    final = work_dir / f"{job.slug}.mp4"
    total = duration(joined)
    _final_pass(job, joined, final, total, edit_cfg, work_dir)
    log.info("[%s] rendered %s (%.1fs)", job.slug, final, total)
    return final


def _final_pass(job: VideoJob, src: Path, dest: Path, total: float, edit_cfg: dict, work_dir: Path) -> None:
    look = LOOKS.get(job.meta.get("look", edit_cfg.get("look", "vintage")), "")
    inputs = ["-i", src]
    vchain = [look] if look else []
    vchain.append("fade=t=in:st=0:d=0.8")
    vchain.append(f"fade=t=out:st={max(total - 1.0, 0):.2f}:d=1.0")

    font = find_font()
    overlay_text = job.meta.get("overlay_text") or job.title
    if edit_cfg.get("title_overlay", True) and font and overlay_text:
        # textfile avoids having to escape quotes/colons in the title
        text_file = work_dir / "overlay.txt"
        text_file.write_text(overlay_text, encoding="utf-8")
        secs = edit_cfg.get("title_overlay_seconds", 4)
        vchain.append(
            f"drawtext=fontfile='{font}':textfile='{text_file}':fontcolor=white:fontsize=h/16"
            f":box=1:boxcolor=black@0.45:boxborderw=24:x=(w-text_w)/2:y=h*0.72"
            f":enable='between(t,0.5,{secs})'"
        )

    filters = [f"[0:v]{','.join(vchain)}[vbase]"]
    vout = "[vbase]"

    logo = ASSETS_DIR / "logo.png"
    if logo.exists():
        inputs += ["-i", logo]
        idx = inputs.count("-i") - 1
        filters.append(f"[{idx}:v]scale=iw*min(1\\,180/iw):-1,format=rgba,colorchannelmixer=aa=0.8[logo]")
        filters.append(f"{vout}[logo]overlay=W-w-40:40[vlogo]")
        vout = "[vlogo]"

    loud = edit_cfg.get("loudness_lufs", -14)
    music = pick_music(job)
    if music:
        inputs += ["-stream_loop", "-1", "-i", music]
        idx = inputs.count("-i") - 1
        vol = job.meta.get("music_volume", edit_cfg.get("music_volume", 0.12))
        filters += [
            "[0:a]asplit=2[voice][sc]",
            f"[{idx}:a]volume={vol},afade=t=out:st={max(total - 2, 0):.2f}:d=2[music]",
            # duck the music whenever the original audio is loud (speech)
            "[music][sc]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=500[ducked]",
            "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"loudnorm=I={loud}:TP=-1.5:LRA=11[aout]",
        ]
    else:
        filters.append(f"[0:a]loudnorm=I={loud}:TP=-1.5:LRA=11[aout]")

    run(["ffmpeg", "-y", *inputs,
         "-filter_complex", ";".join(filters),
         "-map", vout, "-map", "[aout]", "-t", f"{total:.3f}",
         "-c:v", "libx264", "-preset", edit_cfg.get("preset", "medium"),
         "-crf", str(edit_cfg.get("crf", 20)), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", dest])


def extract_frame(video: Path, dest: Path, at_fraction: float = 0.3) -> Path:
    t = duration(video) * at_fraction
    run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-q:v", "2", dest])
    return dest
