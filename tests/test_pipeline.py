import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from pipeline import metadata
from pipeline.config import load_channel_config, load_job
from pipeline.metadata import History, generate_metadata, jaccard, to_hashtag

CONFIG = load_channel_config()


def make_job(tmp_path: Path, slug: str, **meta):
    folder = tmp_path / slug
    folder.mkdir()
    base = {"title": f"{slug} title", "topic": slug.replace("-", " "), "summary": f"story about {slug}"}
    base.update(meta)
    (folder / "meta.yml").write_text(yaml.safe_dump(base, allow_unicode=True))
    return load_job(folder, CONFIG)


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_to_hashtag():
    assert to_hashtag("gaon ki holi") == "#GaonKiHoli"
    assert to_hashtag("#90s kids") == "#90sKids"
    assert to_hashtag("Doordarshan") == "#Doordarshan"
    assert to_hashtag("गाँव") is not None
    assert to_hashtag("!!") is None


def test_every_video_gets_unique_hashtags_and_description(tmp_path):
    history = History(tmp_path / "history.json")
    topics = {
        "gaon-ki-holi": ["holi", "gujiya", "thandai"],
        "doordarshan-sunday": ["doordarshan", "rangoli", "mahabharat"],
        "school-ki-chhutti": ["school", "tiffin", "slate"],
        "monsoon-kagaz-ki-naav": ["barish", "kagaz naav", "monsoon"],
        "gaon-ka-mela": ["mela", "jhoola", "jalebi"],
    }
    produced = []
    for slug, kws in topics.items():
        job = make_job(tmp_path, slug, keywords=kws)
        meta = generate_metadata(job, history)
        m = CONFIG["metadata"]

        assert meta["hashtags"][0] == "#90sGaonChronicles"
        assert len(meta["hashtags"]) <= m["max_hashtags"] <= 15
        assert len({h.lower() for h in meta["hashtags"]}) == len(meta["hashtags"])
        new = [h for h in meta["hashtags"] if history.used(h) == 0 and h != "#90sGaonChronicles"]
        assert len(new) >= m["min_new_hashtags"]
        for prev in history.hashtag_sets():
            assert jaccard({h.lower() for h in meta["hashtags"]}, prev) <= m["max_hashtag_overlap"]

        assert meta["description"].rstrip().endswith(" ".join(meta["hashtags"]))
        assert "<" not in meta["description"] and len(meta["description"]) <= 5000
        assert sum(len(t) + 3 for t in meta["tags"]) <= 520
        assert meta["body"] not in [p["body"] for p in produced]

        meta["video_id"] = f"vid-{slug}"
        history.record(slug, meta)
        produced.append(meta)

    history.save()
    reloaded = History(tmp_path / "history.json")
    assert len(reloaded.videos) == len(topics)
    assert reloaded.hashtag_counts["#90sgaonchronicles"] == len(topics)


def test_similar_description_is_regenerated(tmp_path, monkeypatch):
    history = History(tmp_path / "h.json")
    job = make_job(tmp_path, "gaon-ki-holi")
    first = generate_metadata(job, history)
    history.record("older-video", first)

    calls = []
    real = metadata.template_generate

    def fake(job, attempt):
        calls.append(attempt)
        out = real(job, attempt)
        if attempt == 0:
            out["body"] = first["body"]  # exact copy of an earlier description
        return out

    monkeypatch.setattr(metadata, "template_generate", fake)
    second = generate_metadata(job, history)
    assert len(calls) >= 2
    assert second["body"] != first["body"]


def test_title_from_meta_wins_unless_ai_title(tmp_path):
    job = make_job(tmp_path, "x-video", title="My <Exact> Title")
    meta = generate_metadata(job, History(tmp_path / "h.json"))
    assert meta["title"] == "My Exact Title"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_edit_and_thumbnail_end_to_end(tmp_path):
    from pipeline.editor import duration, edit_video, extract_frame, probe
    from pipeline.thumbnail import build_thumbnail

    job = make_job(tmp_path, "render-test", overlay_text="Gaon Ki Holi")
    # landscape clip with audio, portrait clip without audio, and a photo
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
                    str(job.folder / "01.mp4")], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=2",
                    str(job.folder / "02.mp4")], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=800x600", "-frames:v", "1",
                    str(job.folder / "03.jpg")], check=True, capture_output=True)
    job = load_job(job.folder, CONFIG)
    job.meta["image_seconds"] = 1.5
    job.config["edit"]["preset"] = "ultrafast"

    out = edit_video(job, tmp_path / "build")
    streams = probe(out)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1920, 1080)
    assert any(s["codec_type"] == "audio" for s in streams)
    assert 5.0 < duration(out) < 6.5

    frame = extract_frame(out, tmp_path / "f.jpg")
    thumb = build_thumbnail(frame, "90s wali Holi", "90sGaonChronicles", tmp_path / "t.jpg")
    assert thumb.stat().st_size < 2 * 1024 * 1024


def test_ai_request_and_parsing(tmp_path, monkeypatch):
    """Runs the Claude call against a mocked HTTP API: checks the request we send and the parsing."""
    import json as _json

    import anthropic
    import httpx2 as httpx  # the HTTP client used by anthropic>=1.0

    sent = {}
    reply = {"title": "Holi 1996 🎨", "body": "Ek dum naya description.", "thumbnail_text": "90s HOLI",
             "hashtags": ["#HoliInTheVillage", "#TesuKeRang", "#GujiyaDay"], "tags": ["village holi 90s"]}

    def handler(request: httpx.Request):
        sent["body"] = _json.loads(request.content)
        sent["beta"] = request.headers.get("anthropic-beta")
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": _json.dumps(reply)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    real_client = anthropic.Anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda: real_client(
        api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    job = make_job(tmp_path, "gaon-ki-holi", ai_title=True, keywords=["holi"])
    meta = generate_metadata(job, History(tmp_path / "h.json"))

    assert sent["body"]["model"] == "claude-opus-5"
    assert sent["body"]["output_config"]["format"]["type"] == "json_schema"
    assert sent["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent["beta"]
    assert meta["generator"] == "claude"
    assert meta["title"] == "Holi 1996 🎨"
    assert "#TesuKeRang" in meta["hashtags"]


def test_share_links_become_direct_downloads():
    from pipeline.editor import direct_url

    fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    for link in (f"https://drive.google.com/file/d/{fid}/view?usp=sharing",
                 f"https://drive.google.com/open?id={fid}",
                 f"https://drive.google.com/uc?export=download&id={fid}"):
        assert direct_url(link) == f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    assert direct_url("https://www.dropbox.com/s/abc/clip.mp4?dl=0") == "https://www.dropbox.com/s/abc/clip.mp4?dl=1"
    assert direct_url("https://example.com/a.mp4") == "https://example.com/a.mp4"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_zip_of_clips_is_rendered_in_order_then_deleted(tmp_path):
    import zipfile

    from pipeline.editor import cleanup_sources, duration, edit_video, find_cover

    src = tmp_path / "src"
    src.mkdir()
    for name, secs in (("2.mp4", 1), ("10.mp4", 2)):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=30:duration={secs}",
                        str(src / name)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360", "-frames:v", "1",
                    str(src / "cover.jpg")], check=True, capture_output=True)
    job = make_job(tmp_path, "zip-episode")
    with zipfile.ZipFile(job.folder / "day.zip", "w") as zf:
        for f in src.iterdir():
            zf.write(f, f"clips/{f.name}")
        zf.writestr("__MACOSX/clips/._2.mp4", "junk")
    job = load_job(job.folder, CONFIG)
    assert [p.name for p in job.clips] == ["day.zip"]
    job.config["edit"]["preset"] = "ultrafast"

    work = tmp_path / "build"
    out = edit_video(job, work)
    assert 2.8 < duration(out) < 3.3          # 1s + 2s clips, cover.jpg not used as a clip
    assert find_cover(work).name == "cover.jpg"

    (work / "abcd1234_day.zip").write_bytes(b"downloaded copy")
    cleanup_sources(work)
    assert not list(work.glob("*.zip")) and not list(work.glob("unzipped_*"))
    assert out.exists()


def test_hand_written_description_is_used_as_is(tmp_path):
    job = make_job(tmp_path, "manual", description="Meri apni likhi kahani.", hashtags=["#MeraGaon"],
                   keywords=["gaon", "kahani"])
    meta = generate_metadata(job, History(tmp_path / "h.json"))
    assert meta["generator"] == "manual"
    assert meta["description"].startswith("Meri apni likhi kahani.")
    assert "#MeraGaon" in meta["hashtags"] and meta["hashtags"][0] == "#90sGaonChronicles"


def test_publish_makes_preview_public_and_trashes_drive_zip(tmp_path, monkeypatch):
    from pipeline import run, uploader

    calls = {}
    monkeypatch.setattr(uploader, "set_privacy", lambda vid, privacy, at=None: calls.update(privacy=(vid, privacy, at)))
    monkeypatch.setattr(uploader, "trash_drive_files", lambda urls: calls.update(trashed=urls) or [])
    monkeypatch.setattr(run, "PUBLISHED_PATH", tmp_path / "published.json")

    link = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view?usp=sharing"
    job = make_job(tmp_path, "ep1", clips=[link])
    published = {"ep1": {"status": "preview", "video_id": "abc", "url": "https://youtu.be/abc", "title": "t"}}
    run.publish(job, published)

    assert calls["privacy"] == ("abc", "public", None)
    assert calls["trashed"] == [link]
    assert published["ep1"]["status"] == "published"
