# Copyright (c) 2025 relakkes@gmail.com
# This module adapts the Kuaishou profile-feed/signing flow from MediaCrawler.
# Source: https://github.com/NanmiCoder/MediaCrawler
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1.
# Keep this notice and LICENSE terms with copies or modifications.
# Use only for non-commercial learning/research, obey platform terms, and avoid
# large-scale crawling or disruptive request rates.

import asyncio
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

log = logging.getLogger("kuaishou")
router = APIRouter(prefix="/kuaishou", tags=["Kuaishou"])

SIGN_CAPTURE_SCRIPT = r"""
(() => {
  if (window.__ks_realm) return;
  let done = false;
  const setter = function (v) {
    if (!done && this && typeof this === "object" && this !== window &&
        typeof this.$encode === "function" &&
        typeof this.$getCatVersion === "function") {
      done = true;
      window.__ks_realm = this;
      try { delete Object.prototype.caver; } catch (e) {}
    }
    Object.defineProperty(this, "caver", {
      value: v, writable: true, enumerable: true, configurable: true,
    });
  };
  try {
    Object.defineProperty(Object.prototype, "caver", { set: setter, configurable: true });
  } catch (e) {}
})();
"""

class ProfileScanRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    cookie: str = Field(min_length=8, max_length=16000)
    max_pages: int = Field(default=5, ge=1, le=10)

def _user_id_from_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in (
        "kuaishou.com", "www.kuaishou.com", "m.kuaishou.com"
    ):
        raise HTTPException(status_code=400, detail="Chỉ chấp nhận URL hồ sơ Kuaishou chính thức.")
    match = re.search(r"/profile/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise HTTPException(status_code=400, detail="URL không chứa mã hồ sơ /profile/ hợp lệ.")
    return match.group(1)

def _cookie_pairs(raw: str) -> list[dict[str, Any]]:
    result = []
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) or not value:
            continue
        result.append({"name": name, "value": value, "domain": ".kuaishou.com", "path": "/", "secure": True})
    if not result:
        raise HTTPException(status_code=401, detail="Cookie phiên Kuaishou không hợp lệ hoặc đang trống.")
    return result

def _profile_scan(user_id: str, cookie: str, max_pages: int) -> dict:
    cookie_pairs = _cookie_pairs(cookie)
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    videos: list[dict] = []
    seen: set[str] = set()
    profile: dict[str, Any] = {"user_id": user_id}
    cursor = ""
    pages_checked = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=user_agent, viewport={"width": 1365, "height": 900})
        context.add_cookies(cookie_pairs)
        page = context.new_page()
        page.add_init_script(SIGN_CAPTURE_SCRIPT)
        try:
            page.goto("https://www.kuaishou.com/?isHome=1", wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_function("() => !!window.__ks_realm", timeout=20000)
            except PlaywrightTimeoutError:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                page.wait_for_function("() => !!window.__ks_realm", timeout=20000)

            session = requests.Session()
            session.headers.update({
                "User-Agent": user_agent,
                "Origin": "https://www.kuaishou.com",
                "Referer": "https://www.kuaishou.com/",
                "Content-Type": "application/json;charset=UTF-8",
                "Cookie": cookie,
            })
            endpoint = "/rest/v/profile/feed"
            for page_number in range(max_pages):
                body = {"user_id": user_id, "pcursor": cursor, "page": "profile"}
                signature = page.evaluate(
                    """([u, q, b]) => new Promise((resolve, reject) => {
                      if (!window.__ks_realm) return reject(new Error("Kuaishou signing environment is unavailable"));
                      window.__ks_realm.call('$encode', [
                        { url: u, query: q, form: {}, requestBody: b },
                        { suc: s => resolve(s), err: e => reject(new Error(String(e))) }
                      ]);
                    })""",
                    [endpoint, {"caver": 2}, body],
                )
                response = session.post(
                    "https://www.kuaishou.com" + endpoint,
                    params={"__NS_hxfalcon": signature, "caver": "2"},
                    data=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
                    timeout=(10, 30),
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("result") != 1:
                    code = payload.get("result")
                    if code == 2:
                        raise HTTPException(status_code=429, detail="Kuaishou đang giới hạn yêu cầu. Hãy đợi rồi thử lại.")
                    if code == 50:
                        raise HTTPException(status_code=401, detail="Phiên đăng nhập hoặc chữ ký Kuaishou không hợp lệ. Hãy đăng nhập lại.")
                    raise HTTPException(status_code=502, detail=f"Kuaishou trả lỗi API: {code}")
                pages_checked += 1
                feeds = payload.get("feeds") or []
                for feed in feeds:
                    if not isinstance(feed, dict):
                        continue
                    photo = feed.get("photo") or {}
                    author = feed.get("author") or {}
                    video_id = str(photo.get("id") or "").strip()
                    if not video_id or video_id in seen:
                        continue
                    seen.add(video_id)
                    profile["display_name"] = author.get("name") or profile.get("display_name")
                    profile["avatar_url"] = author.get("headerUrl") or author.get("avatar") or profile.get("avatar_url")
                    videos.append({
                        "id": video_id,
                        "url": "https://www.kuaishou.com/short-video/" + video_id,
                        "title": photo.get("caption") or photo.get("title") or "",
                        "description": photo.get("caption") or "",
                        "published_at": photo.get("timestamp") or photo.get("time"),
                    })
                next_cursor = str(payload.get("pcursor") or "")
                if not feeds or not next_cursor or next_cursor == cursor:
                    cursor = ""
                    break
                cursor = next_cursor
                if page_number + 1 < max_pages:
                    time.sleep(2.0)
        finally:
            context.close()
            browser.close()

    return {
        "status": "success",
        "platform": "KUAISHOU",
        "profile": profile,
        "videos": videos,
        "video_count": len(videos),
        "pages_checked": pages_checked,
        "has_more": bool(cursor),
        "next_cursor": cursor or None,
    }

@router.post("/profile/videos")
def scan_profile_videos(request: ProfileScanRequest):
    user_id = _user_id_from_url(request.url)
    try:
        return _profile_scan(user_id, request.cookie, request.max_pages)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Kuaishou profile scan failed")
        message = str(exc).strip()[:300] or type(exc).__name__
        raise HTTPException(status_code=502, detail=f"Không quét được hồ sơ Kuaishou: {message}")
