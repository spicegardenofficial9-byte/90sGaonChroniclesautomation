# 90s Gaon Chronicles: YouTube automation

Push raw clips to this repo. GitHub Actions then edits the video, writes a
unique title, description and hashtag set, makes a thumbnail, and uploads
everything to YouTube.

```
videos/inbox/<episode>/  ──►  edit (ffmpeg)  ──►  metadata (Claude / templates)  ──►  thumbnail  ──►  YouTube upload
   clips + meta.yml           retro look, music,     unique description + hashtags     1280x720        scheduled/private/public
                              title, logo, loudness  checked against history
```

## What each step does

| Step | File | Details |
|---|---|---|
| Edit | `pipeline/editor.py` | Normalizes every clip to 1920x1080 (or 1080x1920 for Shorts). Portrait phone clips get a blurred background. Photos get a slow Ken Burns zoom. Adds `assets/intro.mp4` / `assets/outro.mp4`, a `vintage` or `vhs` 90s look, fade in/out, the title on screen and the `assets/logo.png` watermark. Background music from `assets/music/` ducks automatically under speech, and loudness is normalized to −14 LUFS. |
| Description + hashtags | `pipeline/metadata.py` | Uses Claude (`claude-opus-5`) when `ANTHROPIC_API_KEY` is set. It sees your episode notes plus the channel's earlier titles, openings and overused hashtags, so it writes something different each time. Without a key it falls back to built-in Hinglish templates. |
| Uniqueness checks | `pipeline/metadata.py` | A description more than 60% similar to any earlier one is regenerated. Every hashtag set has the brand tag, at least 3 hashtags the channel has never used, the least-used pool tags, and at most 50% overlap with any earlier video. The limit is 12 hashtags, since YouTube drops all of them above 15. History lives in `data/metadata_history.json`. |
| Thumbnail | `pipeline/thumbnail.py` | Takes a frame (or your `cover.jpg`), adds a warm retro tint, big outlined text and a channel badge. |
| Upload | `pipeline/uploader.py` | YouTube Data API v3 with a resumable upload and retries, then sets the thumbnail, the optional playlist and the optional `publish_at` schedule. |
| Orchestration | `pipeline/run.py` | Skips videos already in `data/published.json`. Saves the generated metadata to `videos/processed/<episode>/`. |
| Automation | `.github/workflows/youtube-pipeline.yml` | Started manually for one episode (`preview`, `publish`, `update-metadata`, `dry-run`). Commits the history back to the repo and attaches the rendered MP4 as a workflow artifact. |

## One-time setup

1. **YouTube API access**
   1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**.
   2. Under **OAuth consent screen**, choose External and add your Google account as a test user. Publish the app to avoid the 7-day token expiry for apps in testing.
   3. Under **Credentials**, create an OAuth client ID of type **Desktop app**. Download it as `scripts/client_secret.json`. This file is gitignored.
   4. On your own computer, run:
      ```bash
      pip install google-auth-oauthlib
      python scripts/get_youtube_token.py
      ```
      Sign in with the account that owns the channel.
2. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`: printed by the script above
   - `ANTHROPIC_API_KEY`: optional. Enables AI-written descriptions; without it the templates are used.
3. **Git LFS for footage**: run `git lfs install` once. `.gitattributes` already routes mp4/mov/mp3 files to LFS.
4. Edit `config/channel.yml`: footer links, email, hashtag pool, default privacy and editing style.
5. Optional assets: `assets/logo.png`, `assets/intro.mp4`, `assets/outro.mp4`, royalty-free tracks in `assets/music/`, and `assets/fonts/title.ttf`.
6. Verify the channel at <https://www.youtube.com/verify> so custom thumbnails are allowed.

> **Important:** until your Google Cloud project passes YouTube's API audit, videos uploaded through the API are **locked as private**. Submit the [audit form](https://support.google.com/youtube/contact/yt_api_form) early. Each upload costs 1,600 of the default 10,000 daily quota units, about 6 uploads a day.

## Daily flow

Every video goes out in two steps, so nothing is public before it has been checked:

1. **Preview.** The day's clips are put in a ZIP on Google Drive, shared as "Anyone with the link".
   An episode folder `videos/inbox/<slug>/meta.yml` gets the ZIP link, title and description.
   Running the workflow with **mode = preview** downloads and unzips the clips, edits them, and uploads
   the video to YouTube as **Private** with its description, hashtags and thumbnail. It then deletes
   the downloaded ZIP from the runner.
2. **Publish.** After the private video has been checked in YouTube Studio, running **mode = publish**
   makes it public (or scheduled if `publish_at` is set) and moves the ZIP on Google Drive to the trash.

Other modes: **update-metadata** re-applies an edited title or description without re-rendering.
**preview + force** re-renders and replaces the private preview. **dry-run** renders without uploading.

> Moving the ZIP to the Drive trash needs a refresh token that also has the Drive permission, and the
> **Google Drive API** enabled in the Cloud project. Otherwise publish still works, and the run summary
> reminds you to delete the ZIP by hand.

### `meta.yml` fields

The required fields are `title`, `topic` and `summary`. The richer `highlights`, `keywords`, `location` and `year` are, the more specific the description and hashtags will be. See `videos/inbox/example-episode/meta.yml` for every option: Shorts format, scheduling, chapters, music, look, thumbnail text and per-video config overrides.

## Running locally

```bash
sudo apt install ffmpeg        # or: brew install ffmpeg
pip install -r requirements.txt
python -m pipeline.run --metadata-only            # preview titles/descriptions/hashtags
python -m pipeline.run --dry-run --slug gaon-ki-holi   # render to build/gaon-ki-holi/
python -m pipeline.run                            # full run incl. upload (needs YT_* env vars)
pip install pytest && pytest -q
```
