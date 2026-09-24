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

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]
RETRYABLE_STATUS = {500, 502, 503, 504}


def youtube_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    missing = [k for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing YouTube credentials: {', '.join(missing)}")
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


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
