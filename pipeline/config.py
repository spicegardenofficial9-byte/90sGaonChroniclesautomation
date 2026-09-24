"""Loads channel config and per-video meta.yml, merging the two."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "channel.yml"
INBOX_DIR = ROOT / "videos" / "inbox"
PROCESSED_DIR = ROOT / "videos" / "processed"
ASSETS_DIR = ROOT / "assets"
DATA_DIR = ROOT / "data"
BUILD_DIR = ROOT / "build"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_channel_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class VideoJob:
    slug: str
    folder: Path
    meta: dict
    config: dict  # channel config merged with the meta.yml `overrides:` block
    clips: list = field(default_factory=list)  # local paths or URLs, in order

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.slug.replace("-", " ").title()


def load_job(folder: Path, channel_config: dict) -> VideoJob:
    meta_path = folder / "meta.yml"
    if not meta_path.exists():
        raise FileNotFoundError(f"{meta_path} is missing")
    with open(meta_path, encoding="utf-8") as fh:
        meta = yaml.safe_load(fh) or {}

    config = deep_merge(channel_config, meta.get("overrides") or {})

    clips = []
    for entry in meta.get("clips") or []:
        if isinstance(entry, str) and entry.startswith(("http://", "https://")):
            clips.append(entry)
        else:
            clips.append(folder / entry)
    if not clips:
        # No explicit list: every video/photo/ZIP in the folder, sorted by name (01_, 02_, ...).
        # Files named thumbnail/cover are only used for the thumbnail.
        skip = {meta.get("thumbnail_image"), "thumbnail.jpg", "thumbnail.png", "cover.jpg", "cover.png"}
        clips = sorted(p for p in folder.iterdir()
                       if p.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS | {".zip"} and p.name not in skip)

    return VideoJob(slug=folder.name, folder=folder, meta=meta, config=config, clips=clips)


def discover_jobs(channel_config: dict, only_slug: str | None = None) -> list[VideoJob]:
    jobs = []
    if not INBOX_DIR.exists():
        return jobs
    for folder in sorted(p for p in INBOX_DIR.iterdir() if p.is_dir()):
        if only_slug and folder.name != only_slug:
            continue
        if not (folder / "meta.yml").exists():
            continue
        job = load_job(folder, channel_config)
        if job.meta.get("skip"):
            continue
        jobs.append(job)
    return jobs
