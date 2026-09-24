"""YouTube Data API v3 uploader (resumable upload + thumbnail + playlist).

Auth uses an OAuth refresh token, so it can run unattended in GitHub Actions.
Create one once with `python scripts/get_youtube_token.py`, then store
YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN as repository secrets.
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {500, 502, 503, 504}


def _credentials():
    from google.oauth2.credentials import Credentials

    missing = [k for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing YouTube credentials: {', '.join(missing)}")
    # no `scopes=`: the refresh keeps whatever the token was granted (asking for a
    # scope the token lacks, e.g. Drive, would make every refresh fail)
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return creds


def youtube_client():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_credentials(), cache_discovery=False)


def _resumable(request) -> dict:
    from googleapiclient.errors import HttpError

    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("upload %d%%", int(status.progress() * 100))
        except HttpError as err:
            if err.resp.status not in RETRYABLE_STATUS or retries >= 8:
                raise
            retries += 1
            wait = min(2 ** retries, 64) + random.random()
            log.warning("HTTP %s, retrying in %.1fs", err.resp.status, wait)
            time.sleep(wait)
        except (ConnectionError, TimeoutError, OSError) as err:
            if retries >= 8:
                raise
            retries += 1
            wait = min(2 ** retries, 64) + random.random()
            log.warning("%s, retrying in %.1fs", err, wait)
            time.sleep(wait)
    return response


def upload_video(video: Path, thumbnail: Path | None, meta: dict, upload_cfg: dict,
                 publish_at: str | None = None) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    youtube = youtube_client()
    status = {
        "privacyStatus": upload_cfg.get("privacy_status", "private"),
        "selfDeclaredMadeForKids": bool(upload_cfg.get("made_for_kids", False)),
        "containsSyntheticMedia": bool(upload_cfg.get("contains_synthetic_media", False)),
    }
    if publish_at:
        # scheduled videos must be uploaded as private; YouTube makes them public at publishAt
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": str(upload_cfg.get("category_id", "22")),
            "defaultLanguage": upload_cfg.get("default_language", "hi"),
            "defaultAudioLanguage": upload_cfg.get("default_language", "hi"),
        },
        "status": status,
    }

    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=16 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media,
        notifySubscribers=bool(upload_cfg.get("notify_subscribers", True)),
    )
    log.info("Uploading %s", video.name)
    video_id = _resumable(request)["id"]
    log.info("Uploaded: https://youtu.be/%s", video_id)

    if thumbnail and thumbnail.exists():
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail))).execute()
        except HttpError as err:
            # needs a phone-verified channel; the upload itself still succeeded
            log.warning("Thumbnail not set (%s). Verify the channel at youtube.com/verify", err)

    playlist_id = upload_cfg.get("playlist_id")
    if playlist_id:
        youtube.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }}).execute()
    return video_id


def set_privacy(video_id: str, privacy: str, publish_at: str | None = None) -> None:
    """Change a preview (private) video to public/unlisted, or schedule it."""
    youtube = youtube_client()
    items = youtube.videos().list(part="status", id=video_id).execute().get("items", [])
    if not items:
        raise RuntimeError(f"Video {video_id} not found on the channel")
    current = items[0]["status"]
    status = {
        "privacyStatus": "private" if publish_at else privacy,
        "selfDeclaredMadeForKids": current.get("selfDeclaredMadeForKids", False),
        "containsSyntheticMedia": current.get("containsSyntheticMedia", False),
        "embeddable": current.get("embeddable", True),
        "license": current.get("license", "youtube"),
        "publicStatsViewable": current.get("publicStatsViewable", True),
    }
    if publish_at:
        status["publishAt"] = publish_at
    youtube.videos().update(part="status", body={"id": video_id, "status": status}).execute()
    after = youtube.videos().list(part="status", id=video_id).execute()["items"][0]["status"]
    if not publish_at and after.get("privacyStatus") != privacy:
        # unaudited API projects can't make videos public
        raise RuntimeError(
            f"YouTube kept the video {after.get('privacyStatus')}. Until the API audit is approved, "
            "publish it by hand in YouTube Studio.")


def update_metadata(video_id: str, meta: dict, upload_cfg: dict, thumbnail: Path | None = None) -> None:
    from googleapiclient.http import MediaFileUpload

    youtube = youtube_client()
    youtube.videos().update(part="snippet", body={"id": video_id, "snippet": {
        "title": meta["title"],
        "description": meta["description"],
        "tags": meta["tags"],
        "categoryId": str(upload_cfg.get("category_id", "22")),
        "defaultLanguage": upload_cfg.get("default_language", "hi"),
        "defaultAudioLanguage": upload_cfg.get("default_language", "hi"),
    }}).execute()
    if thumbnail and thumbnail.exists():
        youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail))).execute()


def delete_video(video_id: str) -> None:
    youtube_client().videos().delete(id=video_id).execute()


def trash_drive_files(urls: list[str]) -> list[str]:
    """Move shared Google Drive files (the day's ZIP) to the Drive trash.

    Needs a refresh token that also has the Drive scope; otherwise it only
    logs a warning. Returns the links that could not be trashed.
    """
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    from .editor import DRIVE_ID

    failed = []
    drive_links = [u for u in urls if "drive.google.com" in u or "drive.usercontent.google.com" in u]
    if not drive_links:
        return failed
    drive = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
    for url in drive_links:
        match = DRIVE_ID.search(url)
        if not match:
            failed.append(url)
            continue
        try:
            drive.files().update(fileId=match.group(1), body={"trashed": True},
                                 supportsAllDrives=True).execute()
            log.info("Moved %s to the Drive trash", url)
        except HttpError as err:
            log.warning("Could not delete %s from Drive (%s). Delete it by hand, or re-create the "
                        "token with the Drive permission (scripts/get_youtube_token.py).", url, err)
            failed.append(url)
    return failed
