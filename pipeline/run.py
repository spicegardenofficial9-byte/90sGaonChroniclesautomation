"""Pipeline entry point: edit → metadata → thumbnail → upload for each pending video.

    python -m pipeline.run                     # process pending videos (up to max_videos_per_run)
    python -m pipeline.run --dry-run           # render + generate metadata, no upload
    python -m pipeline.run --metadata-only     # just preview title/description/hashtags
    python -m pipeline.run --slug my-episode   # only this folder in videos/inbox/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .config import BUILD_DIR, DATA_DIR, IMAGE_EXTS, PROCESSED_DIR, VIDEO_EXTS, discover_jobs, load_channel_config
from .metadata import History, generate_metadata

log = logging.getLogger("pipeline")
PUBLISHED_PATH = DATA_DIR / "published.json"


def load_published() -> dict:
    return json.loads(PUBLISHED_PATH.read_text()) if PUBLISHED_PATH.exists() else {}


def save_published(data: dict) -> None:
    PUBLISHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUBLISHED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def to_utc_rfc3339(value) -> str | None:
    """'2026-10-01 18:30 +05:30' / datetime from YAML → '2026-10-01T13:00:00Z'."""
    if not value:
        return None
    when = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))  # assume IST
    when = when.astimezone(dt.timezone.utc)
    if when <= dt.datetime.now(dt.timezone.utc):
        log.warning("publish_at %s is in the past; uploading without a schedule", value)
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def pick_thumbnail_base(job, video: Path | None, work_dir: Path) -> Path | None:
    custom = job.meta.get("thumbnail_image")
    if custom and (job.folder / custom).exists():
        return job.folder / custom
    if video:
        from .editor import extract_frame
        at = float(job.meta.get("thumbnail_at", 0.3))
        # the joined cut has no burned-in title/logo, so the thumbnail text won't clash
        clean = work_dir / "joined.mp4"
        return extract_frame(clean if clean.exists() else video, work_dir / "frame.jpg", at)
    photos = [c for c in job.clips if isinstance(c, Path) and c.suffix.lower() in IMAGE_EXTS]
    return photos[0] if photos else None


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def process(job, args, history: History, published: dict) -> None:
    work_dir = BUILD_DIR / job.slug
    out_dir = PROCESSED_DIR / job.slug
    work_dir.mkdir(parents=True, exist_ok=True)

    video = None
    if not args.metadata_only:
        from .editor import edit_video
        video = edit_video(job, work_dir)

    meta = generate_metadata(job, history)

    thumb = None
    base = pick_thumbnail_base(job, video, work_dir)
    if base:
        from .thumbnail import build_thumbnail
        brand = (job.config["channel"].get("brand_hashtags") or [""])[0].lstrip("#")
        thumb = build_thumbnail(base, meta["thumbnail_text"], brand, work_dir / "thumbnail.jpg")

    video_id = None
    if not (args.dry_run or args.metadata_only):
        from .uploader import upload_video
        video_id = upload_video(video, thumb, meta, job.config.get("upload", {}),
                                publish_at=to_utc_rfc3339(job.meta.get("publish_at")))
        meta["video_id"] = video_id
        history.record(job.slug, meta)
        history.save()
        published[job.slug] = {
            "video_id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "title": meta["title"],
            "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        save_published(published)
        if job.config.get("pipeline", {}).get("cleanup_inbox_after_upload"):
            for f in job.folder.iterdir():
                if f.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS:
                    f.unlink()

    # human-readable record of what was generated (committed back by the workflow)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "description.txt").write_text(meta["description"] + "\n", encoding="utf-8")
    if thumb:
        shutil.copy(thumb, out_dir / "thumbnail.jpg")

    status = f"https://youtu.be/{video_id}" if video_id else ("dry run" if args.dry_run else "metadata only")
    log.info("[%s] done: %s", job.slug, status)
    write_summary([
        f"## {meta['title']}",
        f"- Status: {status}",
        f"- Generator: {meta['generator']} (similarity to earlier descriptions: {meta['similarity']})",
        f"- Hashtags: {' '.join(meta['hashtags'])}",
        "", "<details><summary>Description</summary>", "", "```", meta["description"], "```", "</details>", "",
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", help="only process videos/inbox/<slug>")
    parser.add_argument("--max-videos", type=int, help="override pipeline.max_videos_per_run")
    parser.add_argument("--dry-run", action="store_true", help="render and generate metadata but do not upload")
    parser.add_argument("--metadata-only", action="store_true", help="skip rendering; only generate metadata")
    parser.add_argument("--force", action="store_true", help="re-process videos that were already uploaded")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    config = load_channel_config()
    published = load_published()
    jobs = [j for j in discover_jobs(config, args.slug) if args.force or j.slug not in published]
    limit = args.max_videos or int(config.get("pipeline", {}).get("max_videos_per_run", 1))
    if not jobs:
        log.info("Nothing to do: no pending folders in videos/inbox/")
        return 0

    history = History()
    failures = 0
    for job in jobs[:limit]:
        log.info("=== %s ===", job.slug)
        try:
            process(job, args, history, published)
        except Exception:
            failures += 1
            log.exception("[%s] failed", job.slug)
            write_summary([f"## ❌ {job.slug} failed. See the logs."])
    if len(jobs) > limit:
        log.info("%d more video(s) pending; they will go out on the next runs", len(jobs) - limit)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
