import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def test_gen_download_success_stops_fallback(monkeypatch):
    monkeypatch.setattr(main, "validate_video_url", lambda value: value)
    monkeypatch.setattr(main, "provider_gendownload", lambda url: {
        "status": "success", "provider": "GenDownload", "video_url": "https://cdn.example/video.mp4"
    })
    monkeypatch.setattr(main, "provider_webdownloader", lambda url: (_ for _ in ()).throw(AssertionError("must not run")))
    result = main.download_with_failover("https://www.tiktok.com/@user/video/123")
    assert result["status"] == "success"
    assert result["provider"] == "GenDownload"
    assert result["attempted_providers"] == ["GenDownload"]


def test_fallback_runs_after_primary_failure(monkeypatch):
    monkeypatch.setattr(main, "validate_video_url", lambda value: value)
    monkeypatch.setattr(main, "provider_gendownload", lambda url: (_ for _ in ()).throw(TimeoutError("timeout")))
    monkeypatch.setattr(main, "provider_webdownloader", lambda url: {
        "status": "success", "provider": "WebDownloader", "video_url": "https://cdn.example/video.mp4"
    })
    result = main.download_with_failover("https://www.youtube.com/watch?v=abc")
    assert result["status"] == "success"
    assert result["provider"] == "WebDownloader"
    assert result["attempted_providers"] == ["GenDownload", "WebDownloader"]


def test_tiktok_worker_is_only_added_for_tiktok(monkeypatch):
    monkeypatch.setattr(main, "validate_video_url", lambda value: value)
    called = []
    def fail(name):
        def inner(url):
            called.append(name)
            raise RuntimeError("offline")
        return inner
    monkeypatch.setattr(main, "provider_gendownload", fail("GenDownload"))
    monkeypatch.setattr(main, "provider_webdownloader", fail("WebDownloader"))
    monkeypatch.setattr(main, "provider_tiktok_worker", fail("TikTok Worker"))
    monkeypatch.setattr(main, "provider_ytdlp", fail("yt-dlp"))
    result = main.download_with_failover("https://www.youtube.com/watch?v=abc")
    assert result["status"] == "error"
    assert called == ["GenDownload", "WebDownloader", "yt-dlp"]


def test_all_fallbacks_are_attempted_for_tiktok(monkeypatch):
    monkeypatch.setattr(main, "validate_video_url", lambda value: value)
    called = []
    def fail(name):
        def inner(url):
            called.append(name)
            raise RuntimeError("offline")
        return inner
    monkeypatch.setattr(main, "provider_gendownload", fail("GenDownload"))
    monkeypatch.setattr(main, "provider_webdownloader", fail("WebDownloader"))
    monkeypatch.setattr(main, "provider_tiktok_worker", fail("TikTok Worker"))
    monkeypatch.setattr(main, "provider_ytdlp", fail("yt-dlp"))
    result = main.download_with_failover("https://www.tiktok.com/@user/video/123")
    assert result["status"] == "error"
    assert called == ["GenDownload", "WebDownloader", "TikTok Worker", "yt-dlp"]


def test_rejects_non_http_and_unknown_hosts():
    for url in ("file:///etc/passwd", "https://example.com/video.mp4"):
        try:
            main.validate_video_url(url)
        except Exception:
            pass
        else:
            raise AssertionError(f"URL should be rejected: {url}")
