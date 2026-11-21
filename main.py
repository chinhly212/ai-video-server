import os
import re
import ipaddress
import socket
import logging
import time
from urllib.parse import urlparse
from typing import Optional

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-video-server")

app = FastAPI(title="AI Video Server", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (compatible; AIVideoServer/0.2)"})

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)

class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)

ALLOWED_HOST_SUFFIXES = (
    "tiktok.com", "tiktokv.com", "tiktokcdn.com", "douyin.com",
    "iesdouyin.com", "kuaishou.com", "gifshow.com", "youtube.com",
    "youtu.be", "googlevideo.com", "instagram.com", "cdninstagram.com",
    "facebook.com", "fb.watch", "fbcdn.net", "threads.net", "x.com",
    "twitter.com", "redd.it", "reddit.com", "bilibili.com", "b23.tv",
)

def validate_video_url(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL phải bắt đầu bằng http:// hoặc https://")
    host = parsed.hostname.lower().rstrip(".")
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail="Tên miền này chưa nằm trong danh sách nền tảng hỗ trợ.")
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

    def get_download_link(payload: dict) -> Optional[str]:
        return (
            payload.get("downloadUrl") or payload.get("download_url")
            or payload.get("url") or payload.get("fileUrl")
        )

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
            download_url = get_download_link(state)
            status = str(state.get("status", "")).lower()
            if status == "completed" and download_url:
                data = state
                break
            if status in ("failed", "error", "cancelled"):
                raise RuntimeError(state.get("error") or state.get("message") or "WebDownloader xử lý thất bại.")
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

def provider_ytdlp(url: str) -> dict:
    options = {
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 1,
        "extractor_retries": 1,
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
        "download_providers": ["GenDownload", "WebDownloader", "TikTok Downloader Worker (TikTok only)", "yt-dlp"],
    }

@app.get("/health")
def health_alias():
    return health()

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

@app.post("/download")
def download_endpoint(req: DownloadRequest):
    result = download_with_failover(req.url)
    if result.get("status") != "success":
        return JSONResponse(status_code=502, content=result)
    return result

@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Tin nhắn không được để trống.")

    video_url = extract_video_id_url(message)
    if video_url:
        result = download_with_failover(video_url)
        if result.get("status") == "success":
            return {
                "type": "video",
                "message": "Đã tìm thấy luồng video. Hãy tải xuống và lưu nội dung bạn có quyền sử dụng.",
                "data": result,
            }
        return {"type": "text", "message": result.get("message"), "errors": result.get("provider_errors", [])}

    if not gemini_client:
        return {
            "type": "text",
            "message": "AI trò chuyện chưa được cấu hình. Chủ máy chủ cần thêm GEMINI_API_KEY trong biến môi trường Render. Chức năng tải video vẫn có thể hoạt động qua API /download."
        }

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=message,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Bạn là trợ lý tiếng Việt của ứng dụng MuChi. Trả lời ngắn gọn, "
                    "chính xác về cách sử dụng ứng dụng. Không tuyên bố đã tải video "
                    "nếu chưa có kết quả từ API tải video."
                ),
                temperature=0.3,
            ),
        )
        answer = getattr(response, "text", None)
        if not answer:
            return {"type": "text", "message": "AI không trả về nội dung. Vui lòng thử lại."}
        return {"type": "text", "message": answer}
    except Exception as exc:
        log.exception("Gemini request failed")
        return {
            "type": "text",
            "message": "Dịch vụ AI đang gặp lỗi hoặc quá tải. Chức năng tải video riêng vẫn có thể dùng qua /download.",
            "error": str(exc)[:300],
        }

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
