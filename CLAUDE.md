# 90s Gaon Chronicles: daily video routine

The channel owner attaches a ZIP of the day's clips in chat, sometimes with a
line about the story. Posting is fully automatic: do NOT ask for approval or
send a preview. Claude writes the metadata; **GitHub Actions** (which holds the
YouTube secrets) edits the video and uploads it to go public at the scheduled
time. No video file ever stays in the repo.

Repo: `spicegardenofficial9-byte/90sGaonChroniclesautomation`. Work branch:
the repo's default branch (`claude/eloquent-ride-0bswjo` unless it changes).

## When a ZIP arrives

1. **Find the ZIP.** Chat attachments are saved under `/tmp/claude-0/`; look for the newest `*.zip`,
   e.g. `find /tmp/claude-0 -name '*.zip' -mmin -120`. If none arrived, say so.
2. **Understand the footage.** Install ffmpeg if missing (`sudo apt-get install -y -q ffmpeg`).
   Unzip to the scratchpad, list the clips, grab a few frames
   (`ffmpeg -ss 2 -i clip.mp4 -frames:v 1 f.jpg`) and look at them, so the description matches what
   is on screen. Combine that with anything the user wrote. Clips play in name order.
3. **Write `videos/inbox/<slug>/meta.yml`** (slug = `YYYY-MM-DD-short-topic`):
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
   Pull first, then commit and push `meta.yml` to the work branch.
4. **Push the footage to a temporary branch** (split, because GitHub rejects files over 100 MB):
   ```bash
   T=$(mktemp -d) && cd $T && git init -q && git checkout -q -b "footage/<slug>"
   mkdir -p videos/inbox/<slug> && split -b 90m /path/to/the.zip videos/inbox/<slug>/day.zip.part-
   git add -f videos && git commit -qm "footage <slug>"
   git remote add origin "$(git -C /home/user/90sGaonChroniclesautomation remote get-url origin)"
   git push -u origin "footage/<slug>"
   ```
5. **Start the workflow**: GitHub MCP `actions_run_trigger`, method `run_workflow`,
   workflow `youtube-pipeline.yml`, ref = work branch, inputs `{"mode": "direct", "slug": "<slug>"}`.
   The workflow rebuilds the ZIP, edits, uploads (scheduled public), then deletes the footage branch.
6. **Wait for the run** (`actions_list` / `actions_get`; a run takes about 5–20 minutes). On success,
   read the `RESULT <slug>: ...` line from the job log for the link and publish time. On failure,
   read the log and report the exact error. Don't retry more than once.
7. **Clean up locally:** delete the attached ZIP, the unzipped copy and the temp git dir.
8. **Reply briefly:** the title, the YouTube link, when it goes public, and the hashtags.
