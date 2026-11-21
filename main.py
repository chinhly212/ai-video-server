import os
import requests
import yt_dlp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

# Cho phép App Android gọi API không bị chặn
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lấy API Key từ cài đặt trên Render
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class ChatRequest(BaseModel):
    message: str

# --- HÀM TẢI VIDEO 3 NGUỒN CHỐNG QUÁ TẢI GIỜ CAO ĐIỂM ---
def download_video_no_watermark(url: str) -> dict:
    """Tải video không dính logo từ đường link người dùng gửi (TikTok, Douyin, FB, IG...)"""
    
    # NGUỒN 1: GenDownload (Bạn điền đúng URL API của GenDownload nếu có tài khoản/endpoint)
    try:
        res = requests.post("https://gendownload.io", json={"url": url}, timeout=5)
        if res.status_code == 200 and res.json().get("video_url"):
            data = res.json()
            return {
                "status": "success",
                "source": "GenDownload",
                "video_url": data["video_url"],
                "cover": data.get("thumbnail", ""),
                "title": data.get("title", "Video không logo")
            }
    except Exception as e:
        print(f"Nguồn 1 (GenDownload) lỗi/bận: {e}")

    # NGUỒN 2: TikWM Public API (Miễn phí, chuyên trị TikTok & Douyin không logo)
    try:
        res = requests.get(f"https://tikwm.com{url}", timeout=6)
        data = res.json()
        if data.get("code") == 0 and data.get("data", {}).get("play"):
            return {
                "status": "success",
                "source": "TikWM",
                "video_url": data["data"]["play"],
                "cover": data["data"].get("cover", ""),
                "title": data["data"].get("title", "Video TikTok/Douyin")
            }
    except Exception as e:
        print(f"Nguồn 2 (TikWM) lỗi/bận: {e}")

    # NGUỒN 3: Mã nguồn mở yt-dlp chạy trực tiếp trên server (Dự phòng cuối cùng)
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and info.get('url'):
                return {
                    "status": "success",
                    "source": "yt-dlp",
                    "video_url": info['url'],
                    "cover": info.get('thumbnail', ''),
                    "title": info.get('title', 'Video')
                }
    except Exception as e:
        print(f"Nguồn 3 (yt-dlp) lỗi: {e}")

    return {"status": "error", "message": "Các máy chủ tải video hiện đều đang bận, bạn vui lòng thử lại sau ít phút nhé!"}

# --- CỔNG GIAO TIẾP CHÍNH CHO APP ANDROID ---
@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    # Bạn sửa lại thông tin hướng dẫn App của bạn ở đoạn dưới đây
    system_prompt = """
    Bạn là trợ lý ảo chính thức của ứng dụng [Tên App Của Bạn].
    Nhiệm vụ 1: Giải đáp thắc mắc và hướng dẫn người dùng các tính năng của App:
    - Tính năng 1: [Ghi hướng dẫn sử dụng tính năng 1 của bạn vào đây...]
    - Tính năng 2: [Ghi hướng dẫn sử dụng tính năng 2 của bạn vào đây...]
    Nhiệm vụ 2: Mỗi khi người dùng gửi đường link video (TikTok, Douyin, Facebook, Instagram...) hoặc yêu cầu tải video, hãy gọi ngay công cụ `download_video_no_watermark` để lấy video không logo cho họ.
    Trả lời ngắn gọn, thân thiện, dễ hiểu bằng tiếng Việt.
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=req.message,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[download_video_no_watermark],
                temperature=0.3
            )
        )

        # Kiểm tra nếu AI vừa gọi hàm tải video
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_response:
                    result = part.function_response.response
                    if result.get("status") == "success":
                        return {
                            "type": "video",
                            "message": "Video không logo của bạn đã sẵn sàng!",
                            "data": result
                        }
                    else:
                        return {
                            "type": "text",
                            "message": result.get("message", "Không thể tải video này.")
                        }

        # Nếu là chat hướng dẫn thông thường
        return {
            "type": "text",
            "message": response.text
        }
    except Exception as e:
        return {"type": "text", "message": f"Hệ thống đang bận, vui lòng thử lại sau. (Lỗi: {str(e)})"}

# Cổng kiểm tra trạng thái server sống/chết (dùng cho UptimeRobot)
@app.get("/")
def home():
    return {"status": "Server AI & Tai Video dang hoat dong tot!"}
