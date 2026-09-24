"""Title / description / hashtag / tag generation with uniqueness checks.

Each video gets:
  * a description written for that video (Claude when ANTHROPIC_API_KEY is
    set, otherwise a seeded template generator), rejected and rewritten if it
    is too similar to any earlier video's description;
  * a hashtag set that always has the brand tag(s), has at least
    `min_new_hashtags` tags never used on the channel before, prefers the
    least-used pool tags, and never overlaps too much with an earlier video.

Every published video's metadata goes into data/metadata_history.json. The
workflow commits that file back so later runs can check against it.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import logging
import os
import random
import re
import unicodedata
from pathlib import Path

from .config import DATA_DIR, VideoJob

log = logging.getLogger(__name__)

HISTORY_PATH = DATA_DIR / "metadata_history.json"
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 500
YOUTUBE_HASHTAG_LIMIT = 15


# --------------------------------------------------------------------------- history

class History:
    def __init__(self, path: Path = HISTORY_PATH):
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.videos: list[dict] = data.get("videos", [])
        self.hashtag_counts: dict[str, int] = data.get("hashtag_counts", {})

    def used(self, tag: str) -> int:
        return self.hashtag_counts.get(tag.lower(), 0)

    def hashtag_sets(self) -> list[set[str]]:
        return [{t.lower() for t in v.get("hashtags", [])} for v in self.videos]

    def descriptions(self) -> list[str]:
        return [v.get("body", "") for v in self.videos if v.get("body")]

    def record(self, slug: str, meta: dict) -> None:
        self.videos = [v for v in self.videos if v.get("slug") != slug]
        self.videos.append({
            "slug": slug,
            "title": meta["title"],
            "body": meta["body"],
            "hashtags": meta["hashtags"],
            "video_id": meta.get("video_id"),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        })
        counts: dict[str, int] = {}
        for v in self.videos:
            for t in v.get("hashtags", []):
                counts[t.lower()] = counts.get(t.lower(), 0) + 1
        self.hashtag_counts = counts

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"videos": self.videos, "hashtag_counts": self.hashtag_counts},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- hashtag helpers

def to_hashtag(text: str) -> str | None:
    """'gaon ki holi' -> '#GaonKiHoli'. Works for Devanagari too."""
    # letters, digits and combining marks (needed for Devanagari matras) form words
    chars = [c if unicodedata.category(c)[0] in "LNM" else " " for c in (text or "")]
    words = "".join(chars).split()
    if not words:
        return None
    tag = "".join(w[0].upper() + w[1:] if w.isascii() else w for w in words)
    if tag.isdigit() or len(tag) < 3 or len(tag) > 40:
        return None
    return "#" + tag


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def keyword_candidates(job: VideoJob) -> list[str]:
    """Hashtags derived from this video's own title/keywords/topic, so they differ per video."""
    phrases: list[str] = list(job.meta.get("keywords") or [])
    for key in ("topic", "location", "festival", "title"):
        if job.meta.get(key):
            phrases.append(str(job.meta[key]))
    tags: list[str] = []
    for p in phrases:
        base = to_hashtag(p)
        if base:
            tags.append(base)
        # single meaningful words + nostalgic suffix variants
        for word in re.findall(r"\w{4,}", p, flags=re.UNICODE):
            for variant in (word, f"{word} memories", f"{word} 90s", f"gaon {word}", f"{word} ki yaadein"):
                t = to_hashtag(variant)
                if t:
                    tags.append(t)
    return tags


def select_hashtags(job: VideoJob, suggested: list[str], history: History) -> list[str]:
    cfg = job.config.get("metadata", {})
    channel = job.config.get("channel", {})
    max_tags = min(int(cfg.get("max_hashtags", 12)), YOUTUBE_HASHTAG_LIMIT)
    min_new = int(cfg.get("min_new_hashtags", 3))
    max_overlap = float(cfg.get("max_hashtag_overlap", 0.5))
    rng = random.Random(job.slug)

    def dedupe(tags):
        seen, out = set(), []
        for t in tags:
            t = to_hashtag(t)
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    brand = dedupe(channel.get("brand_hashtags", []) + (job.meta.get("hashtags") or []))
    specific = [t for t in dedupe(suggested + keyword_candidates(job)) if t.lower() not in {b.lower() for b in brand}]
    new_tags = [t for t in specific if history.used(t) == 0]
    reused = [t for t in specific if history.used(t) > 0]

    pool = dedupe(channel.get("hashtag_pool", []))
    rng.shuffle(pool)  # tie-break equally-used pool tags differently per video
    pool = sorted((t for t in pool if t.lower() not in {s.lower() for s in brand + specific}), key=history.used)

    chosen = brand[:max_tags]
    # never-used tags go first, since they make the set unique. Leave ~4 slots
    # for the broader pool tags, which help discovery.
    new_limit = max(min_new, max_tags - len(chosen) - 4)
    chosen += new_tags[:max(0, min(new_limit, max_tags - len(chosen)))]
    for t in reused + pool:
        if len(chosen) >= max_tags:
            break
        if t not in chosen:
            chosen.append(t)

    # enforce overlap limit against every earlier video: swap most-used tags for unused ones
    spare = [t for t in new_tags if t not in chosen]
    previous = history.hashtag_sets()
    def worst_overlap():
        s = {c.lower() for c in chosen}
        return max((jaccard(s, p) for p in previous), default=0.0)
    while worst_overlap() > max_overlap and spare:
        swappable = [c for c in chosen if c not in brand]
        if not swappable:
            break
        most_used = max(swappable, key=history.used)
        if history.used(most_used) == 0:
            break
        chosen[chosen.index(most_used)] = spare.pop(0)

    if worst_overlap() > max_overlap:
        log.warning("[%s] hashtag overlap %.2f still above %.2f: add more `keywords` to meta.yml",
                    job.slug, worst_overlap(), max_overlap)
    return chosen


# --------------------------------------------------------------------------- description helpers

def clean(text: str) -> str:
    # YouTube rejects '<' and '>' in titles/descriptions
    return re.sub(r"[<>]", "", text or "").strip()


def max_similarity(body: str, previous: list[str]) -> float:
    norm = lambda s: re.sub(r"\s+", " ", s.lower())
    body_n = norm(body)
    return max((difflib.SequenceMatcher(None, body_n, norm(p)).ratio() for p in previous), default=0.0)


def build_tags(job: VideoJob, suggested: list[str], hashtags: list[str]) -> list[str]:
    """YouTube 'tags' (keywords, not shown publicly), max 500 chars total."""
    raw = list(job.meta.get("keywords") or []) + suggested + [h.lstrip("#") for h in hashtags]
    raw.append(job.config.get("channel", {}).get("name", ""))
    out, seen, total = [], set(), 0
    for t in raw:
        t = clean(str(t)).replace(",", " ").strip()
        if not t or t.lower() in seen or len(t) > 100:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1  # quotes for multi-word tags + comma
        if total + cost > MAX_TAGS_CHARS:
            break
        seen.add(t.lower())
        out.append(t)
        total += cost
    return out


def chapters_block(job: VideoJob) -> str:
    chapters = job.meta.get("chapters") or []
    if not chapters:
        return ""
    lines = [f"{c['time']} {c['title']}" for c in chapters if c.get("time") and c.get("title")]
    return "⏱️ Chapters\n" + "\n".join(lines) if lines else ""


def assemble_description(job: VideoJob, body: str, hashtags: list[str]) -> str:
    parts = [clean(body), chapters_block(job), clean(job.config.get("channel", {}).get("footer", "")),
             " ".join(hashtags)]
    text = "\n\n".join(p for p in parts if p)
    if len(text) > MAX_DESCRIPTION:
        overflow = len(text) - MAX_DESCRIPTION
        body = clean(body)[: max(len(clean(body)) - overflow - 3, 200)] + "..."
        parts[0] = body
        text = "\n\n".join(p for p in parts if p)[:MAX_DESCRIPTION]
    return text


# --------------------------------------------------------------------------- generators

HOOKS = [
    "Yaad hai woh din jab {topic} ka matlab tha poora gaon ek saath? 🌾",
    "Chaliye wapas chalte hain 90s ke gaon mein, jahan {topic} sirf ek cheez nahi, ek ehsaas tha.",
    "Na mobile, na internet, bas {topic} aur dher saari masti. Aaj ki kahani usi zamane ki hai.",
    "Kuch yaadein kabhi purani nahi hoti. {topic} unmein se ek hai. ❤️",
    "Agar aapka bachpan gaon mein beeta hai, toh {topic} ki yeh kahani aapko zaroor rula degi. 🥹",
    "Mitti ki khushboo, chulhe ki roti aur {topic}. Aaj khulte hain 90s ki yaadon ke panne.",
    "Ek zamana tha jab {topic} ke liye hum poore saal intezaar karte the.",
]
MIDDLES = [
    "Is video mein dekhiye {summary}",
    "Aaj ke episode mein: {summary}",
    "Is kahani mein aap dekhenge {summary}",
    "Humne koshish ki hai un palon ko phir se jeene ki: {summary}",
]
QUESTIONS = [
    "{topic} se judi aapki sabse pyaari yaad kya hai? Comments mein zaroor batayein! 👇",
    "Kya aapko bhi {topic} ki koi yaad hai? Neeche likhiye, hum padhenge. 💬",
    "Apne bachpan ki {topic} wali koi kahani ho toh comment karein! 🙏",
    "Aap kis gaon/shehar se hain? Wahan ki {topic} wali yaadein humse share karein! 📍",
]
CTAS = [
    "Video pasand aaye toh Like karein aur apne bachpan ke doston ke saath share karein.",
    "Yeh video un doston ko bhejiye jinke saath aapne yeh din jiye the. 🤝",
    "Aise hi aur 90s ki gaon wali kahaniyon ke liye channel ko subscribe karein.",
]


def template_generate(job: VideoJob, attempt: int) -> dict:
    rng = random.Random(f"{job.slug}:{attempt}")
    topic = job.meta.get("topic") or job.title
    summary = str(job.meta.get("summary") or job.title).strip()
    lines = [rng.choice(HOOKS).format(topic=topic), "", rng.choice(MIDDLES).format(summary=summary.rstrip(".") + ".")]
    highlights = list(job.meta.get("highlights") or [])
    if highlights:
        rng.shuffle(highlights)
        bullet = rng.choice(["✨", "🌾", "📻", "🪁", "🏡"])
        lines += [""] + [f"{bullet} {h}" for h in highlights]
    if job.meta.get("location") or job.meta.get("year"):
        where = ", ".join(str(x) for x in (job.meta.get("location"), job.meta.get("year")) if x)
        lines += ["", f"📍 {where}"]
    lines += ["", rng.choice(QUESTIONS).format(topic=topic), rng.choice(CTAS)]
    return {
        "title": job.meta.get("title") or job.title,
        "body": "\n".join(lines),
        "hashtags": [],
        "tags": [],
        "thumbnail_text": job.meta.get("thumbnail_text") or topic,
    }


SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "YouTube title, max 90 characters, curiosity-driven, no clickbait lies"},
        "body": {"type": "string", "description": "Description text WITHOUT hashtags, chapters or channel footer"},
        "hashtags": {"type": "array", "items": {"type": "string"},
                     "description": "8-12 hashtags specific to THIS video, CamelCase, starting with #"},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "10-20 search keywords/phrases people would type to find this video"},
        "thumbnail_text": {"type": "string", "description": "2-5 punchy words for the thumbnail, Latin script"},
    },
    "required": ["title", "body", "hashtags", "tags", "thumbnail_text"],
    "additionalProperties": False,
}


def ai_generate(job: VideoJob, history: History, attempt: int, feedback: str = "") -> dict:
    import anthropic

    channel = job.config.get("channel", {})
    cfg = job.config.get("metadata", {})
    language = job.meta.get("language") or channel.get("default_language", "hinglish")
    recent = history.videos[-15:]
    overused = sorted(history.hashtag_counts, key=history.hashtag_counts.get, reverse=True)[:25]

    system = (
        f"You write YouTube metadata for the channel \"{channel.get('name')}\".\n"
        f"Channel niche: {channel.get('niche', '').strip()}\n"
        f"Audience: {channel.get('audience', '')}\n"
        f"Tone: {channel.get('tone', '')}\n\n"
        "Write metadata that is specific to the video described by the user: mention its concrete "
        "details, not generic nostalgia filler. Each video's description must read as clearly "
        "different from the channel's earlier descriptions: vary the opening line, structure, "
        "emojis and closing question. Descriptions should be 120-250 words: a strong 1-2 line hook "
        "(this is what shows in search), a short story-style summary, 3-5 highlight lines, and one "
        "question inviting comments. Hashtags must be relevant to this video's specific content; "
        "avoid the channel's overused hashtags listed by the user."
    )
    prompt = {
        "language": language,
        "video": {k: v for k, v in job.meta.items() if k not in ("overrides", "clips")},
        "earlier_videos_do_not_repeat": [
            {"title": v["title"], "opening": v.get("body", "")[:160]} for v in recent
        ],
        "overused_hashtags_to_avoid": overused,
    }
    content = "Generate metadata for this video:\n" + json.dumps(prompt, ensure_ascii=False, indent=2)
    if feedback:
        content += f"\n\nYour previous attempt was rejected: {feedback}"

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=cfg.get("model", "claude-opus-5"),
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        # if a safety classifier declines, the API retries on a fallback model server-side
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request ({response.stop_details})")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude response was truncated")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def generate_metadata(job: VideoJob, history: History, attempts: int = 4) -> dict:
    cfg = job.config.get("metadata", {})
    use_ai = cfg.get("use_ai", True) and bool(os.environ.get("ANTHROPIC_API_KEY"))
    threshold = float(cfg.get("max_description_similarity", 0.6))
    previous = history.descriptions()

    best, best_score, feedback = None, 2.0, ""
    if job.meta.get("description"):
        # written by hand in meta.yml: use it as-is, only warn if it repeats an older one
        best = {"title": job.meta.get("title") or job.title, "body": str(job.meta["description"]),
                "hashtags": [], "tags": [], "_source": "manual",
                "thumbnail_text": job.meta.get("thumbnail_text") or job.meta.get("topic") or job.title}
        best_score = max_similarity(best["body"], previous)
        if best_score > threshold:
            log.warning("[%s] description is %.0f%% similar to an earlier one", job.slug, best_score * 100)
        attempts = 0
    for attempt in range(attempts):
        raw, source = None, "claude"
        if use_ai:
            try:
                raw = ai_generate(job, history, attempt, feedback)
            except Exception as exc:  # network/API/parse issues: fall back instead of failing the upload
                log.warning("[%s] AI generation failed (%s); using templates", job.slug, exc)
                use_ai = False
        if raw is None:
            raw, source = template_generate(job, attempt), "template"
        raw["_source"] = source

        score = max_similarity(raw["body"], previous)
        log.info("[%s] attempt %d: similarity to earlier descriptions %.2f", job.slug, attempt + 1, score)
        if score < best_score:
            best, best_score = raw, score
        if score <= threshold:
            break
        feedback = (f"the description is {score:.0%} similar to an earlier video's description. "
                    "Use a completely different opening, structure and wording.")

    # a title written in meta.yml wins, unless `ai_title: true` asks for a generated one
    if job.meta.get("title") and not job.meta.get("ai_title"):
        title = job.meta["title"]
    else:
        title = best.get("title") or job.title
    title = clean(title)[:MAX_TITLE]
    hashtags = select_hashtags(job, best.get("hashtags", []), history)
    return {
        "title": title,
        "body": clean(best["body"]),
        "description": assemble_description(job, best["body"], hashtags),
        "hashtags": hashtags,
        "tags": build_tags(job, best.get("tags", []), hashtags),
        "thumbnail_text": clean(job.meta.get("thumbnail_text") or best.get("thumbnail_text") or title),
        "similarity": round(best_score if previous else 0.0, 3),
        "generator": best["_source"],
        "fingerprint": hashlib.sha1(best["body"].encode()).hexdigest()[:12],
    }
