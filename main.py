import os
import re
import html as html_lib
import ipaddress
import socket
import logging
import time
import tempfile
import shutil
import mimetypes
from urllib.parse import urlparse, parse_qs
from typing import Optional

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-video-server")

app = FastAPI(title="AI Video Server", version="0.2.0")
from kuaishou_api import router as kuaishou_router
app.include_router(kuaishou_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (compatible; AIVideoServer/0.2)"})

class ChatHistoryItem(BaseModel):
    role: str = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=2000)

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=12)

class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)



class DouyinProfileScanRequest(BaseModel):
    sec_uid: str = Field(min_length=12, max_length=256)
    cookie: str = Field(default="", max_length=16000)
    since_timestamp: int = Field(ge=0)
    max_items: int = Field(default=20, ge=1, le=20)
    cursor: str = Field(default="0", min_length=1, max_length=64)

class VideoProxyRequest(BaseModel):
    video_url: str = Field(min_length=8, max_length=8192)
    source_url: str = Field(min_length=8, max_length=4096)
    provider: str = Field(default="", max_length=120)

ALLOWED_HOST_SUFFIXES = (
    "tiktok.com", "tiktokcdn.com", "douyin.com",
    "iesdouyin.com", "kuaishou.com", "gifshow.com", "youtube.com",
    "youtu.be", "googlevideo.com", "instagram.com", "cdninstagram.com",
    "facebook.com", "fb.watch", "fbcdn.net", "threads.net", "x.com",
    "twitter.com", "redd.it", "reddit.com", "bilibili.com", "b23.tv",
)

def is_supported_video_link(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        path = parsed.path.lower()
        query = parsed.query.lower()

        def host_is(domain: str) -> bool:
            return host == domain or host.endswith("." + domain)

        if host_is("tiktok.com"):
            if host in ("vm.tiktok.com", "vt.tiktok.com"):
                return path not in ("", "/")
            return bool(re.search(r"/@[^/]+/video/[^/?]+|/t/[^/]+|/v/[^/]+", path))
        if host_is("douyin.com") or host_is("iesdouyin.com"):
            if host == "v.douyin.com":
                return path not in ("", "/")
            return bool(re.search(r"/video/[^/?]+|/share/video/[^/?]+", path))
        if host_is("kuaishou.com") or host_is("gifshow.com"):
            if host in ("v.kuaishou.com", "v.gifshow.com"):
                return path not in ("", "/")
            return bool(re.search(r"/short-video/[^/?]+|/fw/photo/[^/?]+|/photo/[^/?]+|/s/[^/?]+|/share/[^/?]+", path))
        if host == "youtu.be":
            return path not in ("", "/")
        if host_is("youtube.com"):
            return (path == "/watch" and bool(re.search(r"(?:^|&)v=[^&]+", query))) or bool(
                re.search(r"/(?:shorts|live|embed)/[^/?]+", path)
            )
        if host_is("instagram.com"):
            return bool(re.search(r"/(?:reel|p|tv)/[^/?]+", path))
        if host == "fb.watch":
            return path not in ("", "/")
        if host_is("facebook.com"):
            return bool(re.search(r"/(?:watch|videos|reel|share/v|share/r)(?:/|$)", path))
        if host_is("threads.net"):
            return bool(re.search(r"/@[^/]+/post/[^/?]+", path))
        if host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com") or host_is("twitter.com"):
            return bool(re.search(r"/[^/]+/status/[^/?]+", path))
        if host == "redd.it":
            return path not in ("", "/")
        if host_is("reddit.com"):
            return bool(re.search(r"/comments/[^/?]+", path))
        if host == "b23.tv":
            return path not in ("", "/")
        if host_is("bilibili.com"):
            return bool(re.search(r"/video/(?:bv[0-9a-z]+|av[0-9]+)", path))
        return False
    except Exception:
        return False


def validate_video_url(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Đường dẫn video không hợp lệ.")
    if not is_supported_video_link(value):
        raise HTTPException(status_code=400, detail="MuChi chỉ nhận link video hợp lệ từ nền tảng được hỗ trợ.")
    host = parsed.hostname.lower().rstrip(".")
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(status_code=400, detail="Địa chỉ máy chủ không được phép.")
    except socket.gaierror:
        pass
    return value


def extract_video_id_url(text: str) -> Optional[str]:
    match = re.search(r"https?://[^\s<>]+", text)
    return match.group(0).rstrip(".,);]") if match else None

def safe_ai_answer(answer: str) -> str:
    value = str(answer or "").strip()
    internal_details = re.compile(
        r"\b(gemini|openrouter|deepseek|api(?:\s*key)?|endpoint|provider|nhà cung cấp mô hình)\b",
        re.IGNORECASE,
    )
    if internal_details.search(value):
        return "Đó là thông tin cấu hình nội bộ của ứng dụng. Tôi có thể hướng dẫn chủ nhân sử dụng các chức năng MuChi."
    return value


def choose_gen_download_format(data: dict) -> Optional[dict]:
    formats = data.get("formats") or []
    videos = [
        item for item in formats
        if isinstance(item, dict)
        and item.get("type") == "video"
        and item.get("url")
        and item.get("ext", "").lower() in ("mp4", "m4v", "mov")
    ]
    if not videos:
        videos = [x for x in formats if isinstance(x, dict) and x.get("type") == "video" and x.get("url")]
    if not videos:
        return None
    def quality(item):
        label = str(item.get("label", ""))
        match = re.search(r"(\d{3,4})", label)
        return int(match.group(1)) if match else 0
    return max(videos, key=quality)

def provider_gendownload(url: str) -> dict:
    r = HTTP.post(
        "https://gendownload.com/api/extract",
        json={"url": url},
        timeout=(8, 35),
    )
    r.raise_for_status()
    data = r.json()
    fmt = choose_gen_download_format(data)
    if not fmt:
        raise RuntimeError("GenDownload không trả về định dạng video.")
    return {
        "status": "success",
        "provider": "GenDownload",
        "title": data.get("title") or "Video",
        "thumbnail": data.get("thumbnail") or "",
        "video_url": fmt["url"],
        "format": fmt.get("label") or fmt.get("ext") or "video",
        "source": data.get("source") or "",
    }

def provider_webdownloader(url: str) -> dict:
    # Public API without API key. Supports queued jobs and temporary signed URLs.
    base = "https://save.brianrianrehan.com"
    r = HTTP.post(
        base + "/api/v1/download",
        params={"wait": "false", "download": "false"},
        json={"url": url, "format": "video", "quality": "best", "outputFormat": "mp4"},
        timeout=(8, 45),
    )
    r.raise_for_status()
    data = r.json()

    def unwrap(payload: dict) -> dict:
        nested = payload.get("data")
        return nested if isinstance(nested, dict) else payload

    def get_download_link(payload: dict) -> Optional[str]:
        payload = unwrap(payload)
        return (
            payload.get("downloadUrl") or payload.get("download_url")
            or payload.get("url") or payload.get("fileUrl")
        )

    data = unwrap(data)
    download_url = get_download_link(data)
    status_url = data.get("statusUrl") or data.get("status_url")
    if not download_url and status_url:
        if status_url.startswith("/"):
            status_url = base + status_url
        parsed_status = urlparse(status_url)
        if parsed_status.scheme != "https" or parsed_status.hostname != "save.brianrianrehan.com":
            raise RuntimeError("WebDownloader trả statusUrl không hợp lệ.")
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            time.sleep(3)
            status_response = HTTP.get(status_url, timeout=(8, 20))
            status_response.raise_for_status()
            state = status_response.json()
            state = unwrap(state)
            download_url = get_download_link(state)
            status = str(state.get("status", "")).lower()
            if status in ("completed", "complete", "success") and download_url:
                data = state
                break
            if status in ("failed", "error", "cancelled"):
                error = state.get("error") or state.get("message") or "WebDownloader xử lý thất bại."
                if isinstance(error, dict):
                    error = error.get("message") or str(error)
                raise RuntimeError(error)
            if status not in ("", "pending", "processing", "downloading", "converting", "completed"):
                raise RuntimeError(f"Trạng thái WebDownloader không nhận diện: {status}")
        else:
            raise RuntimeError("WebDownloader quá thời gian chờ xử lý.")
    if not download_url:
        raise RuntimeError("WebDownloader chưa trả URL tải hợp lệ.")
    parsed_download = urlparse(download_url)
    if parsed_download.scheme not in ("http", "https") or not parsed_download.hostname:
        raise RuntimeError("WebDownloader trả URL tải không hợp lệ.")
    return {
        "status": "success",
        "provider": "WebDownloader",
        "title": data.get("title") or "Video",
        "thumbnail": data.get("thumbnail") or "",
        "video_url": download_url,
        "format": "mp4",
        "source": data.get("platform") or "",
    }

def provider_tiktok_worker(url: str) -> dict:
    host = urlparse(url).hostname or ""
    if not (host == "tiktok.com" or host.endswith(".tiktok.com") or host in ("vt.tiktok.com", "vm.tiktok.com")):
        raise RuntimeError("API dự phòng thứ ba chỉ hỗ trợ TikTok.")
    r = HTTP.get(
        "https://tdownv4.sl-bjs.workers.dev/",
        params={"down": url},
        timeout=(8, 35),
    )
    r.raise_for_status()
    data = r.json()
    video_url = data.get("download_url") or data.get("video_url")
    if not video_url:
        raise RuntimeError("TikTok Worker không trả về download_url.")
    return {
        "status": "success",
        "provider": "TikTok Downloader Worker",
        "title": data.get("title") or "TikTok video",
        "thumbnail": data.get("cover") or data.get("thumbnail") or "",
        "video_url": video_url,
        "format": "mp4",
        "source": "TikTok",
    }

def provider_kuaishou(url: str) -> dict:
    """Resolve a public Kuaishou share page's inline MP4 stream (SSR)."""
    user_agent = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    )
    response = HTTP.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.kuaishou.com/",
        },
        timeout=(10, 25),
    )
    response.raise_for_status()
    page = html_lib.unescape(response.text)
    page = page.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&")
    matches = re.findall(r"""https?://[^\s"'<>]+?\.mp4(?:\?[^\s"'<>]*)?""", page)
    unique = {}
    for candidate in matches:
        candidate = candidate.rstrip("),;")
        key = candidate.split("?", 1)[0]
        unique.setdefault(key, candidate)
    candidates = list(unique.values())
    if not candidates:
        raise RuntimeError("Trang chia sẻ Kuaishou không chứa link MP4; liên kết có thể riêng tư, hết hạn hoặc trang đã đổi cấu trúc.")
    hd = [item for item in candidates if "hd15" in item or "_hd" in item]
    standard = [item for item in candidates if "_b_" in item]
    others = [item for item in candidates if item not in hd and item not in standard]
    video_url = (hd or standard or others)[0]
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "Kuaishou video"
    return {
        "status": "success",
        "provider": "Kuaishou SSR",
        "title": title or "Kuaishou video",
        "thumbnail": "",
        "video_url": video_url,
        "format": "mp4",
        "source": "Kuaishou",
    }


def provider_ytdlp(url: str) -> dict:
    options = {
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 1,
        "extractor_retries": 2,
        # TikTok may require TLS/browser impersonation. curl-cffi is installed
        # through the yt-dlp extra in requirements.txt.
        "impersonate": "chrome",
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("yt-dlp không lấy được thông tin video.")
    video_url = info.get("url")
    if not video_url and info.get("requested_formats"):
        video_url = info["requested_formats"][0].get("url")
    if not video_url:
        raise RuntimeError("yt-dlp không tìm thấy luồng video.")
    return {
        "status": "success",
        "provider": "yt-dlp (server)",
        "title": info.get("title") or "Video",
        "thumbnail": info.get("thumbnail") or "",
        "video_url": video_url,
        "format": info.get("ext") or "video",
        "source": info.get("extractor") or "",
    }

def download_with_failover(raw_url: str) -> dict:
    url = validate_video_url(raw_url)
    providers = [
        ("GenDownload", provider_gendownload),
        ("WebDownloader", provider_webdownloader),
    ]
    host = (urlparse(url).hostname or "").lower()
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        providers.append(("TikTok Downloader Worker", provider_tiktok_worker))
    if (
        host == "kuaishou.com" or host.endswith(".kuaishou.com")
        or host == "gifshow.com" or host.endswith(".gifshow.com")
    ):
        providers.append(("Kuaishou SSR", provider_kuaishou))
    providers.append(("yt-dlp", provider_ytdlp))
    errors = []
    for name, function in providers:
        try:
            result = function(url)
            log.info("Download resolved by %s", name)
            result["attempted_providers"] = [x[0] for x in providers[:len(errors)+1]]
            return result
        except Exception as exc:
            message = f"{name}: {type(exc).__name__}: {str(exc)[:240]}"
            errors.append(message)
            log.warning("Provider failed: %s", message)
    return {
        "status": "error",
        "message": "Tất cả nguồn tải hiện không xử lý được liên kết này.",
        "provider_errors": errors,
    }

@app.get("/")
def health():
    return {
        "service": "AI Video Server",
        "status": "healthy",
        "version": app.version,
        "gemini_configured": bool(GEMINI_API_KEY),
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "deepseek_configured": bool(DEEPSEEK_API_KEY),
        "ai_ready": bool(GEMINI_API_KEY or OPENROUTER_API_KEY or DEEPSEEK_API_KEY),
        "ai_providers": [
            name for name, configured in (
                ("Gemini", GEMINI_API_KEY),
                ("OpenRouter", OPENROUTER_API_KEY),
                ("DeepSeek", DEEPSEEK_API_KEY),
            ) if configured
        ],
        "download_providers": ["GenDownload", "WebDownloader", "TikTok Downloader Worker (TikTok only)", "yt-dlp"],
    }

@app.get("/health")
def health_alias():
    return health()


def _parse_douyin_profile_feed(status: int, payload) -> list[dict]:
    """Validate a Douyin profile-feed response and normalize its video entries."""
    if status < 200 or status >= 300:
        raise ValueError(f"Douyin feed returned HTTP {status}")
    if not isinstance(payload, dict):
        raise ValueError("Douyin feed payload is not a JSON object")
    if "aweme_list" not in payload:
        raise ValueError("Douyin feed payload is missing aweme_list")
    items = payload["aweme_list"]
    if not isinstance(items, list):
        raise ValueError("Douyin feed aweme_list is not a list")

    videos = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("aweme_id") or item.get("id") or "").strip()
        if not video_id:
            continue
        stamp = item.get("create_time")
        try:
            published_at = int(stamp) * 1000 if stamp is not None else None
        except (TypeError, ValueError):
            published_at = None
        description = item.get("desc") or ""
        videos.append({
            "id": video_id,
            "url": item.get("share_url") or f"https://www.douyin.com/video/{video_id}",
            "title": description,
            "description": description,
            "published_at": published_at,
        })
    return videos


def _filter_douyin_profile_videos(collected: dict, since_timestamp: int, max_items: int) -> list[dict]:
    """Apply the requested publication cutoff and result limit to captured videos."""
    videos = []
    for item in collected.values():
        timestamp = item.get("published_at")
        if timestamp is None:
            if since_timestamp > 0:
                continue
        elif timestamp < since_timestamp:
            continue
        videos.append(item)
    videos.sort(key=lambda item: item.get("published_at") or 0, reverse=True)
    return videos[:max_items]


def _open_douyin_profile_page(page, profile_url: str) -> bool:
    """Start profile navigation without waiting for the full Douyin document.

    Douyin keeps long-running page resources open, so waiting for DOM readiness
    can time out even after the browser has received the initial document.
    A navigation timeout is treated as recoverable: the caller still inspects
    any feed responses captured while the page was loading.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.goto(profile_url, wait_until="commit", timeout=45000)
        return True
    except PlaywrightTimeoutError:
        log.warning(
            "Douyin profile navigation did not reach commit within 45s; "
            "continuing to inspect any captured feed responses"
        )
        return False


@app.post("/douyin/profile/videos")
def douyin_profile_videos(req: DouyinProfileScanRequest):
    """Read profile-post data from the real Douyin page in Chromium.

    The page itself creates Douyin's changing request signatures. We observe its
    own profile-feed responses instead of passing a profile URL to yt-dlp.
    """
    from playwright.sync_api import sync_playwright

    sec_uid = req.sec_uid.strip()
    if not re.fullmatch(r"MS4wLjAB[A-Za-z0-9_-]{10,240}", sec_uid):
        raise HTTPException(status_code=400, detail="Mã sec_uid Douyin không hợp lệ.")

    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    cookie_pairs = []
    for part in req.cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) and value:
            cookie_pairs.append({
                "name": name, "value": value, "domain": ".douyin.com",
                "path": "/", "secure": True,
            })

    collected = {}
    feed_responses = 0
    valid_feed_responses = 0
    response_errors = []
    next_cursor = None
    has_more = False

    def capture_profile_feed(response):
        nonlocal feed_responses, valid_feed_responses, next_cursor, has_more
        response_path = urlparse(response.url).path
        if "aweme" not in response_path:
            return
        log.info(
            "Douyin profile response path=%s status=%s content_type=%s",
            response_path, response.status, response.headers.get("content-type", "")
        )
        if "/aweme/v1/web/aweme/post/" not in response_path:
            return
        request_cursor = (parse_qs(urlparse(response.url).query).get("max_cursor") or ["0"])[0]
        if str(request_cursor) != str(req.cursor):
            return
        feed_responses += 1
        try:
            payload = response.json()
            videos = _parse_douyin_profile_feed(response.status, payload)
            next_cursor = str(payload.get("max_cursor") or "") or None
            has_more = bool(int(payload.get("has_more") or 0))
            valid_feed_responses += 1
            log.info(
                "Douyin profile feed parsed keys=%s aweme_count=%s has_more=%s max_cursor_present=%s",
                sorted(payload.keys()),
                len(payload["aweme_list"]),
                payload.get("has_more"),
                bool(payload.get("max_cursor")),
            )
            for video in videos:
                collected[video["id"]] = video
        except Exception as exc:
            response_errors.append(type(exc).__name__)
            log.warning(
                "Douyin profile feed response rejected status=%s error=%s",
                response.status, type(exc).__name__,
            )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=user_agent,
                locale="zh-CN",
                viewport={"width": 1365, "height": 900},
            )
            if cookie_pairs:
                context.add_cookies(cookie_pairs)
            page = context.new_page()
            page.on("response", capture_profile_feed)
            try:
                profile_url = "https://www.douyin.com/user/" + sec_uid
                navigation_committed = _open_douyin_profile_page(page, profile_url)
                page.wait_for_timeout(5000)
                try:
                    page_text = (page.locator("body").inner_text(timeout=1500) or "").lower()
                except Exception as page_text_error:
                    # Page text is diagnostic only; it must never abort video collection.
                    page_text = ""
                    log.warning(
                        "Douyin profile diagnostic text unavailable: %s",
                        type(page_text_error).__name__,
                    )
                log.info(
                    "Douyin profile page state navigation_committed=%s title=%r final_url_path=%s login_hint=%s verify_hint=%s",
                    navigation_committed,
                    page.title(),
                    urlparse(page.url).path,
                    any(word in page_text for word in ("登录", "login", "扫码")),
                    any(word in page_text for word in ("验证", "验证码", "captcha", "安全验证")),
                )
                stagnant_rounds = 0
                previous_count = 0
                for _ in range(20):
                    if valid_feed_responses > 0 or len(collected) >= req.max_items:
                        break
                    page.mouse.wheel(0, 2400)
                    page.wait_for_timeout(1500)
                    current_count = len(collected)
                    stagnant_rounds = stagnant_rounds + 1 if current_count == previous_count else 0
                    previous_count = current_count
                    if stagnant_rounds >= 5:
                        break
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        log.exception("Douyin browser profile scan failed for %s", sec_uid[:18])
        raise HTTPException(
            status_code=502,
            detail=f"Không mở/quét được trang Douyin: {type(exc).__name__}: {str(exc)[:180]}",
        ) from exc

    videos = _filter_douyin_profile_videos(
        collected, req.since_timestamp, req.max_items
    )

    if valid_feed_responses == 0:
        detail = "Douyin không trả về phản hồi danh sách video hợp lệ. Hãy kiểm tra phiên đăng nhập hoặc thử lại sau."
        if feed_responses:
            detail += " Máy chủ đã nhận phản hồi nhưng dữ liệu bị từ chối hoặc không đúng định dạng."
        if response_errors:
            detail += " Một số phản hồi bị lỗi khi đọc."
        raise HTTPException(status_code=502, detail=detail)

    return {
        "status": "success",
        "platform": "DOUYIN",
        "profile_id": sec_uid,
        "videos": videos,
        "video_count": len(videos),
        "feed_responses": feed_responses,
        "has_more": has_more and bool(next_cursor),
        "next_cursor": next_cursor if has_more else None,
    }

@app.get("/providers")
def providers():
    return {
        "primary": {"name": "GenDownload", "free_no_key": True},
        "fallbacks": [
            {"name": "WebDownloader", "free_no_key": True, "rate_limited": True},
            {"name": "TikTok Downloader Worker", "free_no_key": True, "platforms": ["TikTok"]},
            {"name": "yt-dlp", "self_hosted": True},
        ],
        "note": "Public free services may impose limits or change availability without notice."
    }

@app.post("/api/validate")
def validate_api_endpoint(
    req: dict,
    api_key: str = Header(default="", alias="X-Provider-Api-Key"),
):
    provider = str(req.get("provider", "")).strip().lower()
    if provider not in ("gemini", "openrouter", "deepseek"):
        raise HTTPException(status_code=400, detail="Nhà cung cấp chưa được hỗ trợ.")
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="API key đang trống.")
    try:
        if provider == "gemini":
            response = HTTP.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": api_key.strip()},
                params={"pageSize": 1},
                timeout=(8, 20),
            )
        elif provider == "openrouter":
            response = HTTP.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                timeout=(8, 20),
            )
        else:
            response = HTTP.get(
                "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                timeout=(8, 20),
            )
        if response.status_code in (400, 401, 403):
            raise HTTPException(status_code=401, detail="API key không hợp lệ hoặc yêu cầu không được nhà cung cấp chấp nhận.")
        if response.status_code == 429:
            raise HTTPException(status_code=429, detail="Nhà cung cấp đang giới hạn lượt gọi hoặc đã hết hạn mức.")
        if response.status_code >= 500:
            raise HTTPException(status_code=502, detail=f"Máy chủ {provider} đang gặp sự cố (HTTP {response.status_code}).")
        response.raise_for_status()
        return {"valid": True, "provider": provider, "message": f"Kết nối {provider} thành công."}
    except HTTPException:
        raise
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="Hết thời gian chờ khi kiểm tra API.")
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Không thể kết nối nhà cung cấp API. Kiểm tra mạng hoặc thử lại.")


def validate_public_media_url(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL file video không hợp lệ.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URL file video chứa thông tin đăng nhập không được phép.")
    host = parsed.hostname.lower().rstrip(".")
    try:
        infos = socket.getaddrinfo(host, None)
        if not infos:
            raise HTTPException(status_code=400, detail="Không phân giải được máy chủ file video.")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise HTTPException(status_code=400, detail="Máy chủ file video trỏ đến địa chỉ mạng riêng hoặc không an toàn.")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Không phân giải được máy chủ file video.")
    return value


def download_via_ytdlp(source_url: str):
    temp_dir = tempfile.mkdtemp(prefix="muchiai-video-")
    options = {
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "recodevideo": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(source_url, download=True)
            expected = downloader.prepare_filename(info)
        candidates = [
            os.path.join(temp_dir, name)
            for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name)) and not name.endswith((".part", ".ytdl"))
        ]
        if expected and os.path.isfile(expected):
            video_path = expected
        elif candidates:
            video_path = max(candidates, key=os.path.getsize)
        else:
            raise RuntimeError("Bộ tải dự phòng không tạo được dữ liệu video.")
        if os.path.getsize(video_path) <= 0:
            raise RuntimeError("Bộ tải dự phòng trả về video rỗng.")
        media_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
        return FileResponse(
            video_path,
            media_type=media_type,
            filename="MuChi-video.mp4",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


@app.post("/download/proxy")
def download_proxy_endpoint(req: VideoProxyRequest):
    # Always validate the public platform URL before touching a CDN URL.
    validate_video_url(req.source_url)
    media_url = validate_public_media_url(req.video_url)
    source_host = (urlparse(req.source_url).hostname or "").lower()
    if source_host.endswith("kuaishou.com") or source_host.endswith("gifshow.com"):
        referer = "https://www.kuaishou.com/"
        user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    elif source_host.endswith("tiktok.com") or source_host.endswith("tiktokv.com"):
        referer = "https://www.tiktok.com/"
        user_agent = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
    elif source_host.endswith("douyin.com") or source_host.endswith("iesdouyin.com"):
        referer = "https://www.douyin.com/"
        user_agent = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
    else:
        referer = req.source_url
        user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

    headers = {
        "User-Agent": user_agent,
        "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.8",
        "Referer": referer,
    }
    upstream = None
    direct_error = None
    current_url = media_url
    try:
        for _ in range(6):
            validate_public_media_url(current_url)
            upstream = HTTP.get(
                current_url,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=(15, 120),
            )
            if upstream.is_redirect:
                location = upstream.headers.get("Location")
                upstream.close()
                upstream = None
                if not location:
                    direct_error = "CDN chuyển hướng nhưng không cung cấp URL đích."
                    break
                from urllib.parse import urljoin
                current_url = urljoin(current_url, location)
                continue
            if upstream.status_code < 200 or upstream.status_code >= 300:
                direct_error = f"CDN từ chối gửi file video (HTTP {upstream.status_code})."
                upstream.close()
                upstream = None
                break
            content_type = upstream.headers.get("Content-Type", "application/octet-stream").lower()
            if (
                "text/html" in content_type
                or "application/json" in content_type
                or "video/webm" in content_type
                or "video/x-matroska" in content_type
            ):
                direct_error = f"CDN trả định dạng không phù hợp để lưu MP4 ({content_type})."
                upstream.close()
                upstream = None
                break

            def stream_media():
                try:
                    for chunk in upstream.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            yield chunk
                finally:
                    upstream.close()

            response_headers = {"X-Video-Provider": req.provider[:120]}
            content_length = upstream.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                response_headers["Content-Length"] = content_length
            return StreamingResponse(stream_media(), media_type=content_type, headers=response_headers)
        else:
            direct_error = "CDN chuyển hướng quá nhiều lần."
    except HTTPException as exc:
        if exc.status_code < 500:
            if upstream is not None:
                upstream.close()
            raise
        direct_error = str(exc.detail)
    except requests.RequestException as exc:
        direct_error = f"{type(exc).__name__}: {str(exc)[:160]}"
    if upstream is not None:
        upstream.close()
        upstream = None

    # A CDN may reject the server's direct request (for example HTTP 409).
    # Retry by letting yt-dlp download the original public platform page on
    # the server, then stream the finished file and clean it after delivery.
    log.warning("Direct CDN transfer failed; trying yt-dlp fallback: %s", (direct_error or "unknown")[:240])
    try:
        return download_via_ytdlp(req.source_url)
    except Exception as fallback_error:
        log.warning("yt-dlp proxy fallback failed: %s", str(fallback_error)[:240])
        raise HTTPException(
            status_code=502,
            detail="Không thể tải video từ nguồn hiện tại. Hãy thử lại sau hoặc kiểm tra video còn công khai hay không.",
        ) from fallback_error

@app.post("/download")
def download_endpoint(req: DownloadRequest):
    result = download_with_failover(req.url)
    if result.get("status") != "success":
        return JSONResponse(status_code=502, content=result)
    return result

@app.post("/chat")
def chat_endpoint(
    req: ChatRequest,
    gemini_api_key: str = Header(default="", alias="X-Gemini-Api-Key"),
    openrouter_api_key: str = Header(default="", alias="X-OpenRouter-Api-Key"),
    deepseek_api_key: str = Header(default="", alias="X-DeepSeek-Api-Key"),
):
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Tin nhắn không được để trống.")

    urls = re.findall(r"https?://[^\s<>]+", message)
    if urls:
        if len(urls) != 1:
            return {
                "type": "unsupported_link",
                "message": "MuChi chỉ nhận một link video hợp lệ mỗi lần. Chủ nhân gửi lại một link video từ nền tảng được hỗ trợ nhé.",
            }
        video_url = urls[0].rstrip(".,);]")
        if not is_supported_video_link(video_url):
            return {
                "type": "unsupported_link",
                "message": "Đường dẫn này không được hỗ trợ. MuChi chỉ nhận link video hợp lệ từ các nền tảng video được hỗ trợ.",
            }
        result = download_with_failover(video_url)
        if result.get("status") == "success":
            return {
                "type": "video",
                "message": "Tôi đã tìm thấy video.",
                "data": result,
            }
        return {
            "type": "text",
            "message": "Xin lỗi, hiện tại tôi chưa tìm được video từ đường dẫn này.",
            "errors": result.get("provider_errors", []),
        }

    normalized_message = message.lower()
    if re.search(
        r"(chủ\s*(sở hữu|app|ứng dụng)|người\s*tạo|ai\s*(là\s*)?(chủ|tạo|làm).*(muchi|ứng dụng|app))",
        normalized_message,
    ):
        return {"type": "text", "message": "Chủ sở hữu ứng dụng MuChi là Chính Ly."}

    if re.search(r"(hỗ trợ.*nền tảng|nền tảng.*hỗ trợ|bao nhiêu.*nền tảng)", normalized_message):
        return {
            "type": "text",
            "message": (
                "MuChi nhận link video từ 10 nền tảng: TikTok, Douyin, Kuaishou, YouTube, "
                "Instagram, Facebook, Threads, X/Twitter, Reddit và Bilibili. "
                "Tính năng theo dõi và quét kênh hiện hỗ trợ Douyin và Kuaishou. "
                "Video cần ở chế độ công khai và khả dụng thì mới có thể tải."
            ),
        }

    if re.search(r"(đăng nhập|login).*(douyin|kuaishou)|(douyin|kuaishou).*(đăng nhập|login)", normalized_message):
        platform = "Douyin" if "douyin" in normalized_message else "Kuaishou"
        return {
            "type": "text",
            "message": (
                f"Đăng nhập {platform}: 1. Mở Cài đặt > Đăng nhập nền tảng > {platform}. "
                f"2. Đăng nhập trên trang {platform} chính thức mở bên trong MuChi. "
                "3. Khi đăng nhập xong, bấm 'Đã đăng nhập – lưu phiên'. "
                f"4. Vào Cài đặt > Thêm tài khoản, chọn {platform}, nhập ID hoặc URL hồ sơ, "
                "xác minh thông tin rồi bật tài khoản. "
                "5. Vào Công việc > Quét ngay để bắt đầu. "
                "Không gửi mật khẩu hoặc mã xác minh cho tôi qua chat."
            ),
        }

    if re.search(r"(quét video|quét kênh|theo dõi kênh|theo dõi tài khoản)", normalized_message):
        return {
            "type": "text",
            "message": (
                "Quy trình theo dõi và quét video: 1. Đăng nhập Douyin hoặc Kuaishou trong Cài đặt. "
                "2. Chọn Thêm tài khoản, nhập ID hoặc URL hồ sơ và xác minh. "
                "3. Bật tài khoản muốn theo dõi. 4. Vào Công việc > Quét ngay. "
                "5. MuChi lấy video mới từ các tài khoản đang bật, bỏ qua video trùng, "
                "thêm video mới vào hàng đợi và tự chạy các bước xử lý được cấu hình. "
                "Chủ nhân theo dõi tiến trình trong Công việc; nếu có lỗi, xem Thông báo hệ thống ở Trang chủ."
            ),
        }

    if re.search(r"\b(api|gemini|openrouter|deepseek|mô hình ai|mô hình nào|các mô hình|mấy mô hình|bao nhiêu mô hình|nhà cung cấp mô hình|\d+\s*mô hình)\b", normalized_message):
        return {
            "type": "text",
            "message": "Tôi không chia sẻ thông tin cấu hình kỹ thuật nội bộ. Chủ nhân có thể quản lý kết nối trợ lý trong phần Cài đặt.",
        }

    if re.search(
        r"(đánh cắp|lấy trộm|chiếm đoạt|phishing|keylogger|ransomware|mã độc|xâm nhập trái phép|vượt xác thực|bypass đăng nhập)",
        normalized_message,
    ):
        return {
            "type": "text",
            "message": "Xin lỗi chủ nhân, tôi không thể hỗ trợ yêu cầu có thể xâm phạm quyền riêng tư hoặc gây hại.",
        }

    system_prompt = (
        "Bạn là trợ lý AI riêng của ứng dụng MuChi. Chủ sở hữu ứng dụng là Chính Ly. "
        "Chỉ nêu tên Chính Ly khi người dùng hỏi trực tiếp về chủ app, người tạo hoặc chủ sở hữu; "
        "không tự nhắc tên này trong cuộc trò chuyện bình thường. Người đang trò chuyện là chủ nhân; "
        "gọi 'chủ nhân' tự nhiên khi phù hợp, không lạm dụng. "
        "Trả lời bằng tiếng Việt tự nhiên, ấm áp, ngắn gọn, đúng trọng tâm. Không bịa thông tin. "
        "Nếu thiếu dữ kiện, hỏi một câu ngắn để làm rõ. "
        "KIẾN THỨC CHÍNH XÁC VỀ ỨNG DỤNG MUCHI: "
        "MuChi có các khu vực Trang chủ, Công việc, AI, Bộ nhớ và Cài đặt. "
        "Trang chủ hiển thị trạng thái hệ thống, tiến trình công việc và thông báo lỗi; "
        "người dùng có thể xem chi tiết sự cố, đánh dấu đã hiểu hoặc xóa thông báo. "
        "Khu vực Công việc hiển thị hàng đợi video và các trạng thái đang chạy, đang chờ, hoàn thành, lỗi, tạm dừng; "
        "có nút Quét ngay, Tiếp tục tất cả, lọc công việc và điều khiển tạm dừng/tiếp tục từng công việc. "
        "Cài đặt quản lý tài khoản nguồn Douyin và Kuaishou, bật/tắt hoặc xóa tài khoản, "
        "đăng nhập nền tảng, cấp quyền thông báo/lưu trữ và mở cấu hình trợ lý. "
        "Muốn thêm tài khoản theo dõi: đăng nhập nền tảng trước, vào Cài đặt > Thêm tài khoản, "
        "chọn Douyin hoặc Kuaishou, nhập ID hoặc URL hồ sơ, xác minh thông tin rồi bật tài khoản. "
        "Đăng nhập Douyin: Cài đặt > Đăng nhập nền tảng > Douyin; đăng nhập trong trang chính thức "
        "mở bên trong ứng dụng, sau đó bấm 'Đã đăng nhập – lưu phiên'. "
        "Đăng nhập Kuaishou làm tương tự bằng nút Kuaishou. MuChi lưu cookie phiên trên điện thoại; "
        "người dùng tự nhập mật khẩu trên trang chính thức, ứng dụng không yêu cầu gửi mật khẩu vào chat. "
        "Cách quét: bật tài khoản nguồn đã đăng nhập, vào Công việc > Quét ngay; ứng dụng lấy video mới "
        "từ các tài khoản đang bật, bỏ qua video trùng, lưu video mới vào danh sách và tạo công việc xử lý. "
        "Quá trình tự động đi qua các bước được cấu hình như tải video, phân tích/tối ưu, nhận diện giọng nói, "
        "dịch, tạo giọng đọc, phụ đề, ghép, xuất và xác minh; một số bước có thể không chạy nếu tác vụ "
        "hoặc cấu hình không yêu cầu. Nếu lỗi, xem Thông báo hệ thống ở Trang chủ rồi thử tiếp tục công việc. "
        "Khu vực Bộ nhớ hiển thị dung lượng điện thoại, video đã xử lý và các chức năng kết nối Google Drive "
        "để tải lên, tải xuống, làm mới hoặc xóa tệp Drive. "
        "Trợ lý AI hỗ trợ trò chuyện, giải thích tính năng và hướng dẫn thao tác; "
        "trong tab AI có nút 'Xóa toàn bộ' để xóa lịch sử chat đã lưu. "
        "Chức năng tải link video chỉ nhận link video hợp lệ từ các nền tảng sau: TikTok, Douyin, Kuaishou, "
        "YouTube, Instagram, Facebook, Threads, X/Twitter, Reddit và Bilibili. "
        "Không khẳng định mọi video đều tải được: video riêng tư, bị giới hạn vùng, hết hạn hoặc nền tảng đổi cấu trúc "
        "có thể không xử lý được. Tài khoản quét kênh hiện chỉ hỗ trợ Douyin và Kuaishou. "
        "KHÔNG tiết lộ bất kỳ tên API, nhà cung cấp mô hình AI, số lượng mô hình, endpoint, máy chủ, "
        "dịch vụ tải, mã HTTP, log hoặc chi tiết triển khai nội bộ. Nếu bị hỏi về cấu hình kỹ thuật bí mật, "
        "chỉ hướng dẫn người dùng vào mục Cài đặt phù hợp, không nêu tên nhà cung cấp hay khóa. "
        "QUY TẮC LINK: nếu người dùng gửi URL không phải link video hợp lệ thuộc danh sách hỗ trợ, "
        "từ chối ngắn gọn; không mở, không phân tích, không chuyển tiếp URL đó tới công cụ hoặc dịch vụ ngoài. "
        "Không được chấp nhận nhiều link trong cùng một yêu cầu. "
        "AN TOÀN: từ chối ngắn gọn các yêu cầu nhạy cảm xâm phạm quyền riêng tư, đánh cắp thông tin, "
        "vượt bảo vệ tài khoản, lừa đảo, xâm nhập trái phép, tạo mã độc hoặc gây hại. "
        "Không yêu cầu người dùng cung cấp mật khẩu, mã OTP, cookie hoặc khóa bí mật trong chat. "
        "Không để lộ tên nhà cung cấp API, dịch vụ tải, máy chủ, endpoint, mã HTTP, stack trace hoặc chi tiết nội bộ. "
        "Không bịa rằng đã thực hiện hành động. Với link video, ứng dụng sẽ tự báo 'Tôi đã tìm thấy video' "
        "khi đã tìm được nguồn; chỉ xác nhận đã tải và lưu khi ứng dụng xác nhận lưu thành công."
    )
    failures = []
    request_gemini_key = gemini_api_key.strip() or GEMINI_API_KEY
    request_openrouter_key = openrouter_api_key.strip() or OPENROUTER_API_KEY
    request_deepseek_key = deepseek_api_key.strip() or DEEPSEEK_API_KEY
    request_gemini_client = genai.Client(api_key=request_gemini_key) if request_gemini_key else None

    # Primary model: Gemini. Optional OpenAI-compatible providers are tried
    # automatically when their keys are configured in the hosting environment.
    if request_gemini_client:
        try:
            conversation = []
            for turn in req.history[-12:]:
                role = "Chủ nhân" if turn.role == "user" else "Trợ lý MuChi"
                if turn.role in ("user", "assistant"):
                    conversation.append(f"{role}: {turn.content[:2000]}")
            conversation.append(f"Chủ nhân: {message}")
            response = request_gemini_client.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                contents="\n".join(conversation),
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.3,
                ),
            )
            answer = getattr(response, "text", None)
            if answer:
                return {"type": "text", "message": safe_ai_answer(answer), "model_provider": "Gemini"}
            failures.append("Gemini: empty response")
        except Exception as exc:
            log.warning("Gemini failed: %s", str(exc)[:240])
            failures.append(f"Gemini: {str(exc)[:180]}")

    compatible_providers = [
        (
            "OpenRouter",
            request_openrouter_key,
            "https://openrouter.ai/api/v1/chat/completions",
            os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        ),
        (
            "DeepSeek",
            request_deepseek_key,
            "https://api.deepseek.com/chat/completions",
            os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        ),
    ]
    for provider_name, api_key, endpoint, model_name in compatible_providers:
        if not api_key:
            continue
        try:
            response = HTTP.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        *[
                            {"role": turn.role, "content": turn.content[:2000]}
                            for turn in req.history[-12:]
                            if turn.role in ("user", "assistant")
                        ],
                        {"role": "user", "content": message},
                    ],
                    "temperature": 0.3,
                },
                timeout=(10, 60),
            )
            response.raise_for_status()
            payload = response.json()
            answer = payload["choices"][0]["message"]["content"]
            if isinstance(answer, list):
                answer = " ".join(
                    part.get("text", "") for part in answer if isinstance(part, dict)
                )
            if answer and str(answer).strip():
                return {"type": "text", "message": safe_ai_answer(str(answer)), "model_provider": provider_name}
            failures.append(f"{provider_name}: empty response")
        except Exception as exc:
            log.warning("%s failed: %s", provider_name, str(exc)[:240])
            failures.append(f"{provider_name}: {str(exc)[:180]}")

    if not (request_gemini_key or request_openrouter_key or request_deepseek_key):
        message_text = (
            "Trợ lý AI chưa được kết nối. Chủ nhân hãy kiểm tra mục kết nối trợ lý trong Cài đặt. "
            "Chức năng tải video vẫn có thể hoạt động độc lập."
        )
    else:
        message_text = (
            "Các mô hình AI hiện không phản hồi. Hệ thống đã thử các nhà cung cấp "
            "được cấu hình; vui lòng thử lại sau. Chức năng tải video vẫn hoạt động độc lập."
        )
    return {"type": "error", "message": message_text, "provider_errors": failures}

@app.get("/test", response_class=HTMLResponse)
def web_chat_test():
    return HTMLResponse("""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Video Server test</title>
<style>body{font:16px system-ui;max-width:720px;margin:30px auto;padding:0 14px}input{width:100%;padding:12px;box-sizing:border-box}button{padding:12px;margin-top:10px}pre{white-space:pre-wrap;background:#eee;padding:12px;border-radius:8px}</style>
</head><body><h2>Kiểm tra AI Video Server</h2><input id="msg" placeholder="Nhập câu hỏi hoặc dán link video"><button id="send">Gửi thử</button><pre id="out">Sẵn sàng</pre>
<script>
const input=document.getElementById('msg'),out=document.getElementById('out');
document.getElementById('send').onclick=async()=>{const message=input.value.trim();if(!message)return;out.textContent='Đang xử lý...';try{const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message})});const d=await r.json();out.textContent=JSON.stringify(d,null,2)}catch(e){out.textContent='Lỗi kết nối: '+e.message}};
</script></body></html>""")
