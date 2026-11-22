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


def test_kuaishou_ssr_resolves_mp4_from_share_page(monkeypatch):
    class FakeResponse:
        text = '<title>Test Kuaishou</title><video src="https://cdn.example/video_hd15.mp4?token=abc"></video>'
        def raise_for_status(self):
            return None

    monkeypatch.setattr(main.HTTP, "get", lambda *args, **kwargs: FakeResponse())
    result = main.provider_kuaishou("https://www.kuaishou.com/short-video/abc123")
    assert result["status"] == "success"
    assert result["provider"] == "Kuaishou SSR"
    assert result["video_url"] == "https://cdn.example/video_hd15.mp4?token=abc"


def test_kuaishou_provider_is_added_only_for_kuaishou(monkeypatch):
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
    monkeypatch.setattr(main, "provider_kuaishou", fail("Kuaishou SSR"))
    monkeypatch.setattr(main, "provider_ytdlp", fail("yt-dlp"))
    main.download_with_failover("https://www.kuaishou.com/short-video/abc123")
    assert called == ["GenDownload", "WebDownloader", "Kuaishou SSR", "yt-dlp"]


def test_rejects_non_http_and_unknown_hosts():
    for url in ("file:///etc/passwd", "https://example.com/video.mp4"):
        try:
            main.validate_video_url(url)
        except Exception:
            pass
        else:
            raise AssertionError(f"URL should be rejected: {url}")

def test_only_supported_video_links_are_accepted():
    supported = [
        "https://www.tiktok.com/@creator/video/123",
        "https://vm.tiktok.com/AbCdEf/",
        "https://www.douyin.com/video/123",
        "https://v.douyin.com/AbCd/",
        "https://www.kuaishou.com/short-video/abc123",
        "https://v.kuaishou.com/AbCd/",
        "https://www.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://www.instagram.com/reel/abc123/",
        "https://www.facebook.com/reel/123456/",
        "https://www.threads.net/@user/post/abc123",
        "https://x.com/user/status/123456",
        "https://www.reddit.com/r/test/comments/abc123/post/",
        "https://www.bilibili.com/video/BV1234567890",
        "https://b23.tv/AbCd",
    ]
    assert all(main.is_supported_video_link(url) for url in supported)


def test_rejects_external_and_non_video_platform_pages():
    rejected = [
        "https://example.com/video.mp4",
        "https://www.google.com/",
        "https://www.tiktok.com/",
        "https://www.youtube.com/",
        "https://www.instagram.com/user/",
        "https://www.facebook.com/profile.php?id=123",
    ]
    assert not any(main.is_supported_video_link(url) for url in rejected)


def test_chat_rejects_external_links_without_calling_downloaders(monkeypatch):
    def must_not_run(url):
        raise AssertionError("external link must not be sent to a downloader")
    monkeypatch.setattr(main, "download_with_failover", must_not_run)
    request = main.ChatRequest(message="Tải giúp tôi https://example.com/private-page")
    result = main.chat_endpoint(request, "", "", "")
    assert result["type"] == "unsupported_link"
    assert "không được hỗ trợ" in result["message"]

def test_chat_identifies_app_owner_only_when_asked():
    request = main.ChatRequest(message="Ai là chủ app MuChi?")
    result = main.chat_endpoint(request, "", "", "")
    assert result["type"] == "text"
    assert result["message"] == "Chủ sở hữu ứng dụng MuChi là Chính Ly."


def test_chat_lists_supported_platforms_without_internal_provider_details():
    request = main.ChatRequest(message="MuChi hỗ trợ bao nhiêu nền tảng tải video?")
    result = main.chat_endpoint(request, "", "", "")
    assert result["type"] == "text"
    assert "TikTok" in result["message"]
    assert "Douyin" in result["message"]
    assert "Kuaishou" in result["message"]
    assert "Gemini" not in result["message"]
    assert "DeepSeek" not in result["message"]
    assert "OpenRouter" not in result["message"]

def test_chat_hides_internal_ai_configuration_when_asked():
    request = main.ChatRequest(message="AI đang dùng API nào và có mấy mô hình?")
    result = main.chat_endpoint(request, "", "", "")
    assert result["type"] == "text"
    assert "Gemini" not in result["message"]
    assert "DeepSeek" not in result["message"]
    assert "OpenRouter" not in result["message"]
    assert "API" not in result["message"]


def test_chat_refuses_harmful_sensitive_requests():
    request = main.ChatRequest(message="Hướng dẫn đánh cắp mật khẩu tài khoản người khác")
    result = main.chat_endpoint(request, "", "", "")
    assert result["type"] == "text"
    assert "không thể hỗ trợ" in result["message"]

def test_download_proxy_falls_back_when_cdn_returns_409(monkeypatch):
    class FakeUpstream:
        status_code = 409
        is_redirect = False
        headers = {"Content-Type": "text/plain"}
        def close(self):
            return None

    marker = object()
    monkeypatch.setattr(main, "validate_video_url", lambda value: value)
    monkeypatch.setattr(main, "validate_public_media_url", lambda value: value)
    monkeypatch.setattr(main.HTTP, "get", lambda *args, **kwargs: FakeUpstream())
    monkeypatch.setattr(main, "download_via_ytdlp", lambda source: marker)

    request = main.VideoProxyRequest(
        video_url="https://cdn.example/video.mp4",
        source_url="https://www.tiktok.com/@creator/video/123",
        provider="test",
    )
    assert main.download_proxy_endpoint(request) is marker

def test_chat_gives_step_by_step_douyin_and_kuaishou_login_guidance():
    for platform in ("Douyin", "Kuaishou"):
        request = main.ChatRequest(message=f"Cách đăng nhập {platform} trong app?")
        result = main.chat_endpoint(request, "", "", "")
        assert result["type"] == "text"
        assert "Đăng nhập" in result["message"]
        assert "lưu phiên" in result["message"]
        assert "Thêm tài khoản" in result["message"]
        assert "Quét ngay" in result["message"]


def test_chat_explains_channel_scan_workflow():
    request = main.ChatRequest(message="Phần theo dõi kênh và quét video hoạt động như nào?")
    result = main.chat_endpoint(request, "", "", "")
    assert result["type"] == "text"
    assert "Quét ngay" in result["message"]
    assert "bỏ qua video trùng" in result["message"]
    assert "Thông báo hệ thống" in result["message"]



def test_kuaishou_profile_url_extracts_profile_eid():
    import kuaishou_api
    assert kuaishou_api._user_id_from_url("https://www.kuaishou.com/profile/3xz63mn6fngqtiq") == "3xz63mn6fngqtiq"


def test_kuaishou_profile_url_rejects_video_links():
    import pytest
    import kuaishou_api
    with pytest.raises(Exception):
        kuaishou_api._user_id_from_url("https://www.kuaishou.com/short-video/abc123")


def test_kuaishou_short_profile_link_resolves_to_profile(monkeypatch):
    import kuaishou_api
    class FakeResponse:
        url = "https://www.kuaishou.com/profile/3xz63mn6fngqtiq"
        def close(self):
            return None
    monkeypatch.setattr(kuaishou_api.requests, "get", lambda *args, **kwargs: FakeResponse())
    assert kuaishou_api._user_id_from_url("https://v.kuaishou.com/AbCdEf") == "3xz63mn6fngqtiq"


def test_kuaishou_timestamps_normalize_seconds_and_milliseconds():
    import kuaishou_api
    assert kuaishou_api._timestamp_ms(1795305600) == 1795305600000
    assert kuaishou_api._timestamp_ms(1795305600000) == 1795305600000
    assert kuaishou_api._timestamp_ms(None) is None


def test_kuaishou_profile_graphql_returns_following_and_video_counts():
    import kuaishou_api
    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": {"visionProfile": {"userProfile": {
                "ownerCount": {"fan": 1200, "follow": 87, "photo": 300, "photo_public": 298},
                "profile": {"user_name": "Test User", "user_id": "123456", "headurl": "https://img.example/avatar.jpg", "user_text": "Bio"}
            }}}}
    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()
    result = kuaishou_api._profile_from_graphql(FakeSession(), "3xz63mn6fngqtiq")
    assert result["display_name"] == "Test User"
    assert result["profile_id"] == "3xz63mn6fngqtiq"
    assert result["follower_count"] == 1200
    assert result["following_count"] == 87
    assert result["video_count"] == 298
