# 90s Gaon Chronicles: daily video routine

The channel owner attaches a ZIP of the day's clips in chat, sometimes with a
line about the story. Posting is fully automatic: do NOT ask for approval or
send a preview. Edit it, write the metadata, upload it to go public at the
scheduled time, then delete the ZIP.

## When a ZIP arrives

1. **Setup** (each new container):
   `command -v ffmpeg || (sudo apt-get update -q && sudo apt-get install -y -q ffmpeg fonts-dejavu-core)`
   and `pip install -q -r requirements.txt`.
   Check that `YT_CLIENT_ID`, `YT_CLIENT_SECRET` and `YT_REFRESH_TOKEN` are set, without printing them.
   If they are missing, tell the user to add them in the cloud environment settings and start a new session.
2. **Find the ZIP.** Chat attachments are saved under `/tmp/claude-0/` (look for the newest `*.zip`,
   e.g. `find /tmp/claude-0 -name '*.zip' -mmin -120`).
3. **Understand the footage.** Unzip it to the scratchpad and list the clips. Grab a few frames
   (`ffmpeg -ss 2 -i clip.mp4 -frames:v 1 f.jpg`) and look at them so the description matches what
   is actually on screen. Combine that with anything the user wrote.
4. **Create the episode.** Name the slug `YYYY-MM-DD-short-topic`, create
   `videos/inbox/<slug>/`, and **move** the ZIP there as `day.zip`. Write `meta.yml` yourself:
   - `title`: under 70 chars, Hinglish, specific, with 1 emoji
   - `topic`, `summary`, `highlights` (3–5), `keywords` (6–10), `location` or `year` if known
   - `description`: 120–250 words in Hinglish (1–2 line hook, story, highlights, one comment question).
     It must read differently from earlier videos. Check the openings in `data/metadata_history.json`.
   - `hashtags`: 3–5 specific to this video that are not in `data/metadata_history.json`
     (the pipeline adds the brand tag and fills the rest)
   - `thumbnail_text`: 2–4 punchy words in Latin script
   - `language: hinglish`
   - `publish_at` only if the user asked for a specific time. Otherwise the next free daily slot
     (`upload.default_publish_time` in `config/channel.yml`, 18:00 IST) is used.
5. **Run it:** `python -m pipeline.run --slug <slug> --direct`
   This renders the video, uploads it scheduled to go public, and deletes the ZIP, the extracted clips
   and the render. The last line prints `RESULT <slug>: ...` with the link and time.
6. **Commit and push** `videos/inbox/<slug>/meta.yml`, `data/` and `videos/processed/<slug>/`
   (never video files; `.gitignore` blocks them). Delete the unzipped copy in the scratchpad too.
7. **Reply briefly:** the title, the YouTube link, when it goes public, and the hashtags.

If the upload fails, report the exact error. Don't retry more than once.
