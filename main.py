import os
import re
import requests
import yt_dlp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

# Mở quyền truy cập cho mọi ứng dụng Android gọi vào
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
    
    # Làm sạch đường link (phòng trường hợp người dùng copy cả đoạn chữ kèm link từ TikTok/Douyin)
    url_match = re.search(r'(https?://[^\s]+)', url)
    clean_url = url_match.group(1) if url_match else url.strip()

    # NGUỒN 1: TikWM API (Tốc độ cao nhất cho TikTok và Douyin không logo)
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
                "source": "Server 1",
                "video_url": data["data"]["play"],
                "cover": data["data"].get("cover", ""),
                "title": data["data"].get("title", "Video không logo")
            }
    except Exception as e:
        print(f"[Nguồn 1 bận]: {e}")

    # NGUỒN 2: GenDownload / API Dự phòng 2
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
                    "source": "Server 2",
                    "video_url": video_link,
                    "cover": data.get("thumbnail", ""),
                    "title": data.get("title", "Video không logo")
                }
    except Exception as e:
        print(f"[Nguồn 2 bận]: {e}")

    # NGUỒN 3: Engine mã nguồn mở yt-dlp chạy trực tiếp trên Server Render (Chống quá tải giờ cao điểm)
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
                    "source": "Server 3",
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

# --- CỔNG XỬ LÝ TIN NHẮN CHÍNH TỪ APP ANDROID ---
@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    user_msg = req.message.strip()
    if not user_msg:
        return {"type": "text", "message": "Bạn hãy nhập câu hỏi hoặc dán link video cần tải nhé!"}

    # Cấu hình vai trò của AI (Bạn có thể sửa nội dung giới thiệu tính năng App tại đây)
    system_prompt = """
    Bạn là Trợ lý AI thông minh tích hợp trong ứng dụng.
    Bạn có 2 nhiệm vụ chính:
    1. Hướng dẫn sử dụng & giải đáp thắc mắc: Hỗ trợ người dùng tận tình, ngắn gọn, dễ hiểu bằng tiếng Việt về cách dùng các tính năng trong ứng dụng (tải video không logo, quản lý file đã tải, chia sẻ...).
    2. Tải video không logo: Bất cứ khi nào tin nhắn của người dùng có chứa đường link video (http:// hoặc https://) từ TikTok, Douyin, Facebook, Instagram, Threads, YouTube... BẮT BUỘC phải gọi ngay công cụ `download_video_no_watermark` để tải video cho họ, không cần hỏi lại.
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

        # Kiểm tra kết quả trả về từ công cụ tải video
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

        # Nếu là cuộc trò chuyện hỏi đáp bình thường
        return {
            "type": "text",
            "message": response.text or "Tôi có thể giúp gì cho bạn?"
        }

    except Exception as e:
        return {
            "type": "text",
            "message": "Trợ lý AI đang xử lý hơi chậm một chút, bạn vui lòng gửi lại nhé!"
        }

# Endpoint kiểm tra trạng thái server (dùng cho UptimeRobot)
@app.get("/")
def health_check():
    return {"status": "online", "message": "AI & Video Downloader Server is running"}
                    
