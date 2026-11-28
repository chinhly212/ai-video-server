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
    max_items: int = Field(default=600, ge=1, le=600)
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


def _is_douyin_profile_feed_path(response_path: str) -> bool:
    """Recognize profile-feed endpoint variants across Douyin web API revisions.

    Douyin has changed the profile-post endpoint path several times. The caller
    validates the response body (it must contain an aweme_list), so requiring
    the URL to end in /post incorrectly discards valid profile-feed responses.
    """
    normalized = response_path.rstrip("/").lower()
    return "aweme" in normalized


def _douyin_feed_request_cursor(response_url: str) -> str:
    values = parse_qs(urlparse(response_url).query).get("max_cursor") or ["0"]
    return str(values[0])


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
        page.goto(profile_url, wait_until="commit", timeout=15000)
        return True
    except PlaywrightTimeoutError:
        log.warning(
            "Douyin profile navigation did not reach commit within 15s; "
            "continuing to inspect any captured feed responses"
        )
        return False

def _activate_douyin_posts_tab(page) -> Optional[str]:
    """Explicitly activate the creator's public-post tab before scrolling.

    Some Douyin profile layouts render the profile shell first and do not
    request the post feed until the Works tab is selected.
    """
    for label in ("作品", "视频", "Posts", "Videos"):
        try:
            locator = page.get_by_text(label, exact=True).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=2500)
                page.wait_for_timeout(1800)
                return label
        except Exception:
            continue
    return None


def _scan_douyin_with_dtk_api(req: DouyinProfileScanRequest) -> Optional[dict]:
    """Use a separately hosted DTK API instance when configured.

    The DTK instance owns its own logged-in identity pool. The mobile cookie is
    deliberately not forwarded to this third-party service; operators import
    and manage the session in DTK itself.
    """
    base_url = os.getenv("DTK_API_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("DTK_API_KEY", "").strip()
    if not base_url or not api_key:
        return None

    sec_uid = req.sec_uid.strip()
    if not re.fullmatch(r"MS4wLjAB[A-Za-z0-9_-]{10,240}", sec_uid):
        raise HTTPException(status_code=400, detail="Mã sec_uid Douyin không hợp lệ.")

    endpoint = base_url + "/api/v1/douyin/user/posts"
    collected: dict[str, dict] = {}
    cursor: Optional[str] = None
    page_count = 0
    has_more = True

    while has_more and len(collected) < req.max_items and page_count < 30:
        params = {
            "sec_user_id": sec_uid,
            "count": 20,
            "wait": 30,
            "lang": "en",
        }
        if cursor:
            params["cursor"] = cursor

        try:
            response = HTTP.get(
                endpoint,
                params=params,
                headers={
                    "X-API-Key": api_key,
                    "Accept": "application/json",
                    "User-Agent": "MuChi-Douyin-Bridge/1.0",
                },
                timeout=(12, 110),
            )
            response.raise_for_status()
            envelope = response.json()
        except requests.RequestException as exc:
            log.warning("DTK Douyin bridge request failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="Dịch vụ quét Douyin chưa kết nối được. Hãy kiểm tra máy chủ API Douyin.",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="Dịch vụ quét Douyin trả về dữ liệu JSON không hợp lệ.",
            ) from exc

        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            error = envelope.get("error") if isinstance(envelope, dict) else None
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
            log.warning("DTK Douyin bridge rejected page: code=%s", code[:80])
            if code == "IDENTITY_LOGIN_REQUIRED":
                detail = "Máy chủ quét Douyin chưa có phiên đăng nhập hợp lệ. Cần nhập phiên Douyin vào bộ quản lý tài khoản của máy chủ API."
            elif code in ("UPSTREAM_RISK_CONTROL", "UPSTREAM_CHANGED"):
                detail = "Douyin đang giới hạn hoặc thay đổi phản hồi. Hãy chờ rồi quét lại."
            else:
                detail = "Máy chủ API Douyin không lấy được danh sách video."
            if message:
                log.info("DTK Douyin bridge message: %s", message[:240])
            raise HTTPException(status_code=502, detail=detail)

        meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
        page = envelope.get("data")
        if isinstance(page, dict) and page.get("task_id"):
            task_url = base_url + "/api/v1/tasks/" + str(page["task_id"])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                time.sleep(2)
                try:
                    task_response = HTTP.get(task_url, headers={
                        "X-API-Key": api_key, "Accept": "application/json"
                    }, timeout=(12, 30))
                    task_response.raise_for_status()
                    task_envelope = task_response.json()
                except (requests.RequestException, ValueError) as exc:
                    raise HTTPException(
                        status_code=502,
                        detail="Không đọc được tiến trình quét Douyin từ máy chủ API.",
                    ) from exc
                task_data = task_envelope.get("data") if isinstance(task_envelope, dict) else None
                if not isinstance(task_data, dict):
                    raise HTTPException(status_code=502, detail="Trạng thái tác vụ Douyin không hợp lệ.")
                state = str(task_data.get("state", "")).lower()
                if state == "done":
                    page = task_data.get("data")
                    meta = task_data.get("result_meta") if isinstance(task_data.get("result_meta"), dict) else {}
                    break
                if state == "failed":
                    task_error = task_data.get("error") if isinstance(task_data.get("error"), dict) else {}
                    task_code = str(task_error.get("code", ""))
                    if task_code == "IDENTITY_LOGIN_REQUIRED":
                        detail = "Máy chủ quét Douyin chưa có phiên đăng nhập hợp lệ. Cần nhập phiên Douyin vào bộ quản lý tài khoản của máy chủ API."
                    elif task_code in ("UPSTREAM_RISK_CONTROL", "UPSTREAM_CHANGED"):
                        detail = "Douyin đang giới hạn hoặc thay đổi phản hồi. Hãy chờ rồi quét lại."
                    else:
                        detail = str(task_error.get("message") or "Tác vụ quét Douyin thất bại.")
                    raise HTTPException(status_code=502, detail=detail)
            else:
                raise HTTPException(status_code=504, detail="Máy chủ Douyin vẫn đang quét. Hãy thử lại sau ít phút.")

        if meta.get("guest_fallback") is True:
            raise HTTPException(
                status_code=503,
                detail="Máy chủ API đang dùng phiên khách nên danh sách có thể thiếu video mới. Hãy nhập phiên đăng nhập Douyin vào máy chủ API rồi quét lại.",
            )

        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise HTTPException(
                status_code=502,
                detail="Máy chủ API Douyin trả về cấu trúc danh sách không hợp lệ.",
            )

        page_count += 1
        for item in page["items"]:
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("content_id") or "").strip()
            if not video_id:
                continue
            created_at = item.get("created_at")
            published_at = None
            if isinstance(created_at, (int, float)):
                published_at = int(created_at * 1000)
            elif isinstance(created_at, str) and created_at:
                try:
                    from datetime import datetime
                    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    published_at = int(parsed.timestamp() * 1000)
                except (TypeError, ValueError, OverflowError):
                    published_at = None
            if published_at is not None and published_at < req.since_timestamp:
                continue
            description = str(item.get("description") or item.get("title") or "")
            collected.setdefault(video_id, {
                "id": video_id,
                "url": str(item.get("web_url") or f"https://www.douyin.com/video/{video_id}"),
                "title": str(item.get("title") or description),
                "description": description,
                "published_at": published_at,
            })
            if len(collected) >= req.max_items:
                break

        cursor_meta = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
        has_more = bool(page.get("has_more", cursor_meta.get("has_more", False)))
        next_cursor = page.get("cursor") or cursor_meta.get("next")
        if not has_more or not next_cursor or str(next_cursor) == cursor:
            has_more = False
            cursor = None
        else:
            cursor = str(next_cursor)
            time.sleep(0.8)

    videos = sorted(
        collected.values(),
        key=lambda item: item.get("published_at") or 0,
        reverse=True,
    )
    return {
        "status": "success",
        "platform": "DOUYIN",
        "profile_id": sec_uid,
        "videos": videos[:req.max_items],
        "video_count": min(len(videos), req.max_items),
        "feed_responses": page_count,
        "has_more": has_more,
        "next_cursor": cursor if has_more else None,
        "source": "DTK_API",
    }


@app.post("/douyin/profile/videos")
def douyin_profile_videos(req: DouyinProfileScanRequest):
    """Read profile-post data from the real Douyin page in Chromium.

    The page itself creates Douyin's changing request signatures. We observe its
    own profile-feed responses instead of passing a profile URL to yt-dlp.
    """
    # Prefer the separately hosted signing/identity engine when configured.
    # Keep the existing Chromium observer as a fallback for deployments that
    # have not provisioned that service yet.
    dtk_result = _scan_douyin_with_dtk_api(req)
    if dtk_result is not None:
        return dtk_result

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
    observed_aweme_responses = []
    observed_aweme_requests = []
    seen_feed_cursors = set()
    page_diagnostics = {
        "navigation_committed": None,
        "final_path": "",
        "page_title": "",
        "body_hint": "",
        "posts_tab_activated": None,
        "login_hint": False,
        "verify_hint": False,
    }
    failed_requests = []
    next_cursor = None
    has_more = False

    def capture_profile_feed(response):
        nonlocal feed_responses, valid_feed_responses, next_cursor, has_more
        response_path = urlparse(response.url).path
        if "aweme" not in response_path.lower():
            return
        content_type = response.headers.get("content-type", "")
        if len(observed_aweme_responses) < 8:
            observed_aweme_responses.append({
                "path": response_path[:180],
                "status": response.status,
                "content_type": content_type[:80],
            })
        log.info(
            "Douyin profile response path=%s status=%s content_type=%s",
            response_path, response.status, content_type
        )
        # Do not require a literal /post suffix: Douyin has served the
        # creator's aweme_list through different aweme endpoints over time.
        # _parse_douyin_profile_feed below is the strict payload validator.
        if not _is_douyin_profile_feed_path(response_path):
            return
        request_cursor = _douyin_feed_request_cursor(response.url)
        if request_cursor in seen_feed_cursors:
            return
        seen_feed_cursors.add(request_cursor)
        feed_responses += 1
        try:
            payload = response.json()
            videos = _parse_douyin_profile_feed(response.status, payload)
            next_cursor = str(payload.get("max_cursor") or "") or None
            page_has_older_videos = any(
                video.get("published_at") is not None
                and video["published_at"] < req.since_timestamp
                for video in videos
            )
            raw_has_more = payload.get("has_more", 0)
            has_more_flag = (
                raw_has_more is True
                or str(raw_has_more).strip().lower() in ("1", "true", "yes")
            )
            has_more = has_more_flag and not page_has_older_videos
            valid_feed_responses += 1
            log.info(
                "Douyin profile feed parsed cursor=%s keys=%s aweme_count=%s has_more=%s max_cursor_present=%s",
                request_cursor,
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
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                ],
            )
            context = browser.new_context(
                user_agent=user_agent,
                locale="zh-CN",
                viewport={"width": 1365, "height": 900},
            )
            if cookie_pairs:
                context.add_cookies(cookie_pairs)
            page = context.new_page()
            # Keep Douyin scripts and API calls, but avoid downloading heavy
            # visual assets that are not needed to enumerate profile videos.
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_(),
            )
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(15000)
            page.on("response", capture_profile_feed)
            page.on("request", lambda request: observed_aweme_requests.append({
                "method": request.method,
                "path": urlparse(request.url).path[:180],
            }) if "aweme" in urlparse(request.url).path.lower() and len(observed_aweme_requests) < 8 else None)
            page.on("requestfailed", lambda request: failed_requests.append({
                "path": urlparse(request.url).path[:140],
                "failure": str(request.failure or "request failed")[:100],
            }) if len(failed_requests) < 5 else None)
            try:
                profile_url = "https://www.douyin.com/user/" + sec_uid
                navigation_committed = _open_douyin_profile_page(page, profile_url)
                page.wait_for_timeout(1800)
                activated_tab = _activate_douyin_posts_tab(page)
                # Trigger both the page's natural infinite-scroll handler and a
                # direct window scroll; profile feeds may be nested in a page
                # container and ignore a single mouse-wheel event.
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(700)
                except Exception:
                    pass
                try:
                    page_text = (page.locator("body").inner_text(timeout=1500) or "").lower()
                except Exception as page_text_error:
                    page_text = ""
                    log.warning(
                        "Douyin profile diagnostic text unavailable: %s",
                        type(page_text_error).__name__,
                    )
                page_diagnostics.update({
                    "navigation_committed": navigation_committed,
                    "final_path": urlparse(page.url).path[:180],
                    "page_title": (page.title() or "")[:120],
                    "body_hint": re.sub(r"\s+", " ", page_text)[:180],
                    "posts_tab_activated": activated_tab,
                    "login_hint": any(word in page_text for word in ("登录", "login", "扫码")),
                    "verify_hint": any(word in page_text for word in ("验证", "验证码", "captcha", "安全验证")),
                })
                log.info(
                    "Douyin profile page state navigation_committed=%s title=%r final_url_path=%s login_hint=%s verify_hint=%s",
                    navigation_committed,
                    page.title(),
                    page_diagnostics["final_path"],
                    page_diagnostics["login_hint"],
                    page_diagnostics["verify_hint"],
                )
                stagnant_rounds = 0
                previous_count = len(collected)
                # One browser session collects multiple feed pages. This avoids
                # launching Chromium again for every 20-video batch from Android.
                for _ in range(40):
                    if len(collected) >= req.max_items:
                        break
                    if valid_feed_responses > 0 and not has_more:
                        break
                    before_responses = feed_responses
                    page.mouse.wheel(0, 2600)
                    try:
                        page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 2200))")
                    except Exception:
                        pass
                    page.wait_for_timeout(900)
                    current_count = len(collected)
                    if current_count == previous_count and feed_responses == before_responses:
                        stagnant_rounds += 1
                    else:
                        stagnant_rounds = 0
                    previous_count = current_count
                    if stagnant_rounds >= 6:
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
        detail = "Douyin không trả về phản hồi danh sách video hợp lệ."
        if not cookie_pairs:
            detail += " Máy chủ nhận được cookie trống; hãy đăng nhập Douyin và lưu lại phiên."
        elif page_diagnostics["login_hint"]:
            detail += " Trang hồ sơ đang yêu cầu đăng nhập; hãy đăng nhập lại và lưu phiên."
        elif page_diagnostics["verify_hint"]:
            detail += " Douyin đang hiển thị bước xác minh/captcha; hãy thử lại sau."
        elif feed_responses:
            detail += " Đã bắt được API bài đăng nhưng dữ liệu trả về không hợp lệ."
        else:
            detail += " Không bắt được API danh sách bài đăng từ trang hồ sơ."
        detail += (
            f" Chẩn đoán: đường dẫn cuối={page_diagnostics['final_path'] or 'không rõ'};"
            f" tiêu đề={page_diagnostics['page_title'] or 'không rõ'};"
            f" tab bài đăng={page_diagnostics['posts_tab_activated'] or 'chưa kích hoạt'};"
            f" phản hồi aweme={len(observed_aweme_responses)};"
            f" phản hồi khớp={feed_responses}; lỗi đọc={len(response_errors)}."
        )
        if page_diagnostics["body_hint"]:
            detail += f" Nội dung trang={page_diagnostics['body_hint']}."
        if observed_aweme_requests:
            requested = ", ".join(f"{item['method']} {item['path']}" for item in observed_aweme_requests[:6])
            detail += f" Yêu cầu aweme đã gửi: {requested}."
        if failed_requests:
            detail += " Yêu cầu lỗi: " + ", ".join(
                f"{item['path']} ({item['failure']})" for item in failed_requests[:3]
            ) + "."
        if observed_aweme_responses:
            paths = ", ".join(
                f"{item['path']} [HTTP {item['status']}]"
                for item in observed_aweme_responses[:5]
            )
            detail += f" API quan sát được: {paths}."
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