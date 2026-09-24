"""Pipeline entry point.

Every video goes through two stages:
  1. preview:  edit → metadata → thumbnail → upload to YouTube as PRIVATE
  2. publish:  after it has been checked, make it public (or scheduled) and
               move the source ZIP on Google Drive to the trash

    python -m pipeline.run --slug ep --direct     # render + upload, public at the next daily slot
    python -m pipeline.run --slug ep              # preview: render + upload as private
    python -m pipeline.run --publish --slug ep    # make the previewed video public
    python -m pipeline.run --update-metadata --slug ep   # re-apply meta.yml title/description
    python -m pipeline.run --dry-run --slug ep    # render only, no upload
    python -m pipeline.run --metadata-only        # just print title/description/hashtags
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

from .config import (BUILD_DIR, DATA_DIR, IMAGE_EXTS, INBOX_DIR, PROCESSED_DIR, VIDEO_EXTS,
                     discover_jobs, load_channel_config, load_job)
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


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def next_publish_slot(config: dict, published: dict, now: dt.datetime | None = None) -> str | None:
    """Next free day at `upload.default_publish_time` (IST) → RFC3339 UTC, or None to publish now.

    Days that already have a scheduled video are skipped, so several ZIPs sent
    on the same day go out on consecutive days.
    """
    at = str(config.get("upload", {}).get("default_publish_time") or "").strip()
    if not at:
        return None
    hour, minute = (int(x) for x in at.split(":"))
    now = now or dt.datetime.now(dt.timezone.utc)
    taken = {e.get("publish_at") for e in published.values() if e.get("publish_at")}
    day = now.astimezone(IST).date()
    while True:
        slot = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(dt.timezone.utc)
        value = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
        # YouTube needs publishAt a little in the future
        if slot > now + dt.timedelta(minutes=15) and value not in taken:
            return value
        day += dt.timedelta(days=1)


def pick_thumbnail_base(job, video: Path | None, work_dir: Path) -> Path | None:
    custom = job.meta.get("thumbnail_image")
    if custom and (job.folder / custom).exists():
        return job.folder / custom
    from .editor import extract_frame, find_cover
    cover = find_cover(work_dir)  # thumbnail.jpg / cover.jpg inside the ZIP
    if cover:
        return cover
    if video:
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


def save_outputs(job, meta: dict, thumb: Path | None) -> None:
    """Human-readable record of what was generated (committed back by the workflow)."""
    out_dir = PROCESSED_DIR / job.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "description.txt").write_text(meta["description"] + "\n", encoding="utf-8")
    if thumb:
        shutil.copy(thumb, out_dir / "thumbnail.jpg")


def summary_lines(meta: dict, status: str) -> list[str]:
    return [
        f"## {meta['title']}",
        f"- Status: {status}",
        f"- Description by: {meta['generator']} (similarity to earlier descriptions: {meta['similarity']})",
        f"- Hashtags: {' '.join(meta['hashtags'])}",
        "", "<details><summary>Description</summary>", "", "```", meta["description"], "```", "</details>", "",
    ]


def preview(job, args, history: History, published: dict) -> None:
    """Render, then upload as PRIVATE so it can be checked on YouTube before going live."""
    work_dir = BUILD_DIR / job.slug
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

    status = "dry run" if args.dry_run else "metadata only"
    if not (args.dry_run or args.metadata_only):
        from .editor import cleanup_sources
        from .uploader import delete_video, upload_video
        publish_at = None
        if args.direct:
            # straight to public: scheduled for meta.yml publish_at, else the next free daily slot
            publish_at = to_utc_rfc3339(job.meta.get("publish_at")) or next_publish_slot(job.config, published)
            upload_cfg = dict(job.config.get("upload", {}),
                              privacy_status=job.config.get("upload", {}).get("publish_privacy", "public"))
        else:
            upload_cfg = dict(job.config.get("upload", {}), privacy_status="private")
        video_id = upload_video(video, thumb, meta, upload_cfg, publish_at=publish_at)
        old = published.get(job.slug, {})
        if old.get("status") == "preview" and old.get("video_id") not in (None, video_id):
            delete_video(old["video_id"])  # replaced by this new preview
            log.info("Deleted the previous preview %s", old["video_id"])
        meta["video_id"] = video_id
        history.record(job.slug, meta)
        history.save()
        published[job.slug] = {
            "status": ("scheduled" if publish_at else "published") if args.direct else "preview",
            "publish_at": publish_at,
            "video_id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "studio": f"https://studio.youtube.com/video/{video_id}/edit",
            "title": meta["title"],
            "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        save_published(published)
        cleanup_sources(work_dir)  # downloaded ZIP + extracted clips are no longer needed here
        if args.direct or job.config.get("pipeline", {}).get("cleanup_inbox_after_upload"):
            # the day's ZIP/clips are deleted once the video is on YouTube
            for f in job.folder.iterdir():
                if f.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS | {".zip"}:
                    f.unlink()
        if args.direct:
            when = (dt.datetime.fromisoformat(publish_at.replace("Z", "+00:00")).astimezone(IST)
                    .strftime("%d %b %Y, %I:%M %p IST") if publish_at else "now")
            status = f"uploaded, goes PUBLIC {when}: https://youtu.be/{video_id}"
        else:
            status = f"PRIVATE preview uploaded: https://youtu.be/{video_id} (not public yet)"

    save_outputs(job, meta, thumb)
    if args.direct and not (args.dry_run or args.metadata_only):
        shutil.rmtree(work_dir, ignore_errors=True)  # rendered video no longer needed locally
    log.info("[%s] %s", job.slug, status)
    print(f"RESULT {job.slug}: {status}")
    write_summary(summary_lines(meta, status))


def publish(job, published: dict) -> None:
    """Make an approved preview public (or scheduled) and trash its ZIP on Google Drive."""
    from .uploader import set_privacy, trash_drive_files

    entry = published.get(job.slug)
    if not entry or not entry.get("video_id"):
        raise RuntimeError(f"{job.slug} has no preview upload yet; run a preview first")
    publish_at = to_utc_rfc3339(job.meta.get("publish_at"))
    privacy = job.config.get("upload", {}).get("publish_privacy", "public")
    set_privacy(entry["video_id"], privacy, publish_at)

    links = [c for c in job.clips if isinstance(c, str)]
    not_deleted = trash_drive_files(links)
    entry.update({
        "status": "scheduled" if publish_at else "published",
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "publish_at": publish_at,
    })
    save_published(published)
    status = f"scheduled for {publish_at}" if publish_at else privacy.upper()
    log.info("[%s] %s: %s", job.slug, status, entry["url"])
    lines = [f"## {entry['title']}", f"- Now {status}: {entry['url']}"]
    lines.append(f"- ⚠️ Delete the ZIP from Google Drive by hand: {' '.join(not_deleted)}" if not_deleted
                 else "- Source ZIP moved to the Google Drive trash")
    write_summary(lines + [""])


def update_meta(job, history: History, published: dict) -> None:
    """Re-apply the title/description/hashtags from meta.yml to an uploaded video (no re-render)."""
    from .uploader import update_metadata

    entry = published.get(job.slug)
    if not entry or not entry.get("video_id"):
        raise RuntimeError(f"{job.slug} has not been uploaded yet")
    history.videos = [v for v in history.videos if v.get("slug") != job.slug]  # don't compare with itself
    meta = generate_metadata(job, history)
    update_metadata(entry["video_id"], meta, job.config.get("upload", {}))
    meta["video_id"] = entry["video_id"]
    history.record(job.slug, meta)
    history.save()
    entry["title"] = meta["title"]
    save_published(published)
    save_outputs(job, meta, None)
    write_summary(summary_lines(meta, f"metadata updated: {entry['url']}"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", help="only process videos/inbox/<slug>")
    parser.add_argument("--max-videos", type=int, help="override pipeline.max_videos_per_run")
    parser.add_argument("--dry-run", action="store_true", help="render and generate metadata but do not upload")
    parser.add_argument("--metadata-only", action="store_true", help="skip rendering; only generate metadata")
    parser.add_argument("--publish", action="store_true", help="make the previewed --slug video public")
    parser.add_argument("--update-metadata", action="store_true", help="re-apply meta.yml text to the uploaded --slug video")
    parser.add_argument("--direct", action="store_true",
                        help="upload straight to public at the scheduled time (no private preview), then delete the ZIP")
    parser.add_argument("--force", action="store_true", help="re-render and replace an existing preview")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    config = load_channel_config()
    published = load_published()
    history = History()

    if args.publish or args.update_metadata:
        if not args.slug:
            parser.error("--publish/--update-metadata need --slug")
        job = load_job(INBOX_DIR / args.slug, config)
        try:
            publish(job, published) if args.publish else update_meta(job, history, published)
        except Exception:
            log.exception("[%s] failed", job.slug)
            write_summary([f"## ❌ {job.slug} failed. See the logs."])
            return 1
        return 0

    jobs = [j for j in discover_jobs(config, args.slug) if args.force or j.slug not in published]
    limit = args.max_videos or int(config.get("pipeline", {}).get("max_videos_per_run", 1))
    if not jobs:
        log.info("Nothing to do: no new folders in videos/inbox/ (use --force to redo a preview)")
        return 0

    failures = 0
    for job in jobs[:limit]:
        log.info("=== %s ===", job.slug)
        try:
            preview(job, args, history, published)
        except Exception:
            failures += 1
            log.exception("[%s] failed", job.slug)
            write_summary([f"## ❌ {job.slug} failed. See the logs."])
    if len(jobs) > limit:
        log.info("%d more video(s) pending; they will go out on the next runs", len(jobs) - limit)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
