import os
import re
import requests
import yt_dlp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

# Mở quyền truy cập cho mọi ứng dụng Android (Poco) và Web gọi vào
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Khởi tạo Gemini AI từ biến môi trường GEMINI_API_KEY
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class ChatRequest(BaseModel):
    message: str

# --- BỘ TẢI VIDEO KHÔNG LOGO 3 NGUỒN (TỰ ĐỘNG CHUYỂN ĐỔI KHI QUÁ TẢI) ---
def download_video_no_watermark(url: str) -> dict:
    """Công cụ tải video không logo từ link TikTok, Douyin, Facebook, Instagram, Threads..."""
    
    url_match = re.search(r'(https?://[^\s]+)', url)
    clean_url = url_match.group(1) if url_match else url.strip()

    # NGUỒN 1: TikWM API (Chuẩn endpoint /api/?url=)
    try:
        res = requests.get(
            f"https://tikwm.com{clean_url}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6
        )
        data = res.json()
        if data.get("code") == 0 and data.get("data", {}).get("play"):
            return {
                "status": "success",
                "source": "Server 1 (TikWM)",
                "video_url": data["data"]["play"],
                "cover": data["data"].get("cover", ""),
                "title": data["data"].get("title", "Video không logo")
            }
    except Exception as e:
        print(f"[Nguồn 1 bận]: {e}")

    # NGUỒN 2: GenDownload API (Chuẩn endpoint /v1/download)
    try:
        res = requests.post(
            "https://gendownload.io",
            json={"url": clean_url},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6
        )
        if res.status_code == 200:
            data = res.json()
            video_link = data.get("video_url") or data.get("url") or data.get("no_watermark")
            if video_link:
                return {
                    "status": "success",
                    "source": "Server 2 (GenDownload)",
                    "video_url": video_link,
                    "cover": data.get("thumbnail", ""),
                    "title": data.get("title", "Video không logo")
                }
    except Exception as e:
        print(f"[Nguồn 2 bận]: {e}")

    # NGUỒN 3: Engine mã nguồn mở yt-dlp chạy trực tiếp trên Server Render
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
            if info and info.get('url'):
                return {
                    "status": "success",
                    "source": "Server 3 (yt-dlp)",
                    "video_url": info['url'],
                    "cover": info.get('thumbnail', ''),
                    "title": info.get('title', 'Video đã xử lý')
                }
    except Exception as e:
        print(f"[Nguồn 3 lỗi]: {e}")

    return {
        "status": "error",
        "message": "Hiện tại cả 3 máy chủ tải video đều đang quá tải hoặc link video ở chế độ riêng tư. Bạn vui lòng thử lại sau ít phút nhé!"
    }

# --- CỔNG XỬ LÝ TIN NHẮN CHÍNH TỪ APP POCO ---
@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    user_msg = req.message.strip()
    if not user_msg:
        return {"type": "text", "message": "Bạn hãy nhập câu hỏi hoặc dán link video cần tải nhé!"}

    system_prompt = """
    Bạn là Trợ lý AI chính thức tích hợp trong ứng dụng Poco.
    Bạn có 2 nhiệm vụ chính:
    1. Hướng dẫn sử dụng & giải đáp thắc mắc: Hỗ trợ người dùng tận tình, ngắn gọn, dễ hiểu bằng tiếng Việt về cách dùng các tính năng trong ứng dụng Poco (tải video không logo, quản lý file đã tải, chia sẻ...).
    2. Tải video không logo: Bất cứ khi nào tin nhắn của người dùng có chứa đường link video (http:// hoặc https://) từ TikTok, Douyin, Facebook, Instagram, Threads, YouTube... BẮT BUỘC phải gọi ngay công cụ `download_video_no_watermark` để lấy video không logo cho họ, không cần hỏi lại.
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[download_video_no_watermark],
                temperature=0.2
            )
        )

        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_response:
                    result = part.function_response.response
                    if result.get("status") == "success":
                        return {
                            "type": "video",
                            "message": "Video không logo của bạn đã sẵn sàng!",
                            "data": {
                                "title": result.get("title", "Video"),
                                "cover": result.get("cover", ""),
                                "video_url": result.get("video_url", ""),
                                "source": result.get("source", "")
                            }
                        }
                    else:
                        return {
                            "type": "text",
                            "message": result.get("message")
                        }

        return {
            "type": "text",
            "message": response.text or "Tôi có thể giúp gì cho bạn?"
        }

    except Exception as e:
        return {
            "type": "text",
            "message": f"Trợ lý AI đang xử lý hơi chậm một chút, bạn vui lòng gửi lại nhé! ({str(e)})"
        }

# --- GIAO DIỆN THỬ NGHIỆM TÍCH HỢP TRỰC TIẾP TRÊN SERVER (/test) ---
@app.get("/test", response_class=HTMLResponse)
def web_chat_test():
    return """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Trợ Lý AI Poco</title>
        <style>
            * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { margin: 0; background: #f0f2f5; display: flex; flex-direction: column; height: 100vh; }
            .header { background: #1e88e5; color: white; padding: 14px 16px; font-weight: bold; font-size: 16px; display: flex; justify-content: space-between; align-items: center; }
            .status-dot { display: inline-block; width: 8px; height: 8px; background: #69f0ae; border-radius: 50%; margin-right: 6px; }
            .clear-btn { background: rgba(255,255,255,0.2); border: none; color: white; padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; }
            .chat-box { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
            .msg { max-width: 82%; padding: 10px 14px; border-radius: 14px; font-size: 15px; line-height: 1.4; word-wrap: break-word; }
            .user { align-self: flex-end; background: #1e88e5; color: white; border-bottom-right-radius: 4px; }
            .bot { align-self: flex-start; background: white; color: #222; border-bottom-left-radius: 4px; box-shadow: 0 1px 2px rgba(0,0,0,0.1); }
            .video-card { margin-top: 8px; background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 8px; max-width: 260px; }
            .video-card img { width: 100%; height: 150px; object-fit: cover; border-radius: 6px; background: #ddd; }
            .video-title { font-size: 13px; font-weight: 600; margin: 6px 0; color: #333; }
            .dl-btn { display: block; text-align: center; background: #2e7d32; color: white; text-decoration: none; padding: 9px; border-radius: 6px; font-size: 13px; font-weight: bold; }
            .source-tag { font-size: 11px; color: #666; margin-top: 5px; display: block; }
            .loading { font-size: 12px; color: #555; padding: 0 16px 6px; font-style: italic; display: none; }
            .input-area { background: white; padding: 10px; display: flex; gap: 8px; border-top: 1px solid #ddd; }
            input { flex: 1; padding: 10px 14px; border: 1px solid #ccc; border-radius: 20px; outline: none; font-size: 15px; }
            button.send { background: #1e88e5; color: white; border: none; padding: 0 20px; border-radius: 20px; font-weight: bold; cursor: pointer; }
            button.send:disabled { background: #90caf9; }
        </style>
    </head>
    <body>
    <div class="header">
        <div><span class="status-dot"></span>Trợ Lý AI Poco</div>
        <button class="clear-btn" id="clearBtn">Xóa lịch sử</button>
    </div>
    <div class="chat-box" id="chatBox"></div>
    <div class="loading" id="loading">Trợ lý AI đang xử lý yêu cầu...</div>
    <div class="input-area">
        <input type="text" id="msgInput" placeholder="Hỏi cách dùng hoặc dán link video...">
        <button class="send" id="sendBtn">Gửi</button>
    </div>
    <script>
        const chatBox = document.getElementById("chatBox");
        const msgInput = document.getElementById("msgInput");
        const sendBtn = document.getElementById("sendBtn");
        const clearBtn = document.getElementById("clearBtn");
        const loading = document.getElementById("loading");
        
