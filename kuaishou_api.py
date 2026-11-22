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
    since_timestamp: int | None = Field(default=None, ge=0)

def _user_id_from_url(value: str) -> str:
    raw = value.strip()
    parsed = urlparse(raw)
    allowed_hosts = {
        "kuaishou.com", "www.kuaishou.com", "m.kuaishou.com",
        "v.kuaishou.com", "gifshow.com", "www.gifshow.com", "v.gifshow.com",
    }
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in allowed_hosts:
        raise HTTPException(status_code=400, detail="Dán liên kết trang cá nhân Kuaishou hợp lệ.")
    resolved = raw
    if (parsed.hostname or "").lower() in {"v.kuaishou.com", "v.gifshow.com"}:
        try:
            response = requests.get(
                raw,
                headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36"},
                allow_redirects=True,
                timeout=(10, 25),
                stream=True,
            )
            resolved = response.url
            response.close()
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Không mở được liên kết hồ sơ Kuaishou: {exc}") from exc
    final = urlparse(resolved)
    final_host = (final.hostname or "").lower()
    if final_host not in {"kuaishou.com", "www.kuaishou.com", "m.kuaishou.com"}:
        raise HTTPException(status_code=400, detail="Liên kết không dẫn tới trang cá nhân Kuaishou.")
    match = re.search(r"/(?:profile|fw/user)/([A-Za-z0-9_-]{3,128})(?:/|$)", final.path)
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Liên kết này chưa phải trang cá nhân Kuaishou. Hãy mở hồ sơ người dùng rồi sao chép liên kết trang cá nhân, không dùng liên kết video."
        )
    return match.group(1)

def _timestamp_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return stamp * 1000 if stamp < 100_000_000_000 else stamp

def _profile_from_graphql(session: requests.Session, profile_id: str) -> dict[str, Any]:
    query = """query visionProfile($userId: String) {
      visionProfile(userId: $userId) {
        result
        userProfile {
          ownerCount { fan photo follow photo_public }
          profile { gender user_name user_id headurl user_text }
          isFollowing
        }
      }
    }"""
    try:
        response = session.post(
            "https://www.kuaishou.com/graphql",
            json={"operationName": "visionProfile", "variables": {"userId": profile_id}, "query": query},
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        data = (payload.get("data") or {}).get("visionProfile") or {}
        user_profile = data.get("userProfile") or {}
        owner = user_profile.get("ownerCount") or {}
        profile = user_profile.get("profile") or {}
        name = profile.get("user_name")
        if not name:
            return {}
        return {
            "display_name": name,
            "profile_id": profile_id,
            "user_id": str(profile.get("user_id") or profile_id),
            "avatar_url": profile.get("headurl"),
            "description": profile.get("user_text"),
            "follower_count": _safe_int(owner.get("fan")),
            "following_count": _safe_int(owner.get("follow")),
            "video_count": _safe_int(owner.get("photo_public") or owner.get("photo")),
        }
    except Exception as exc:
        log.warning("Kuaishou profile metadata lookup failed: %s", exc)
        return {}

def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None

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

def _profile_scan(
    user_id: str,
    cookie: str,
    max_pages: int,
    since_timestamp: int | None = None,
) -> dict:
    cookie_pairs = _cookie_pairs(cookie)
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    videos: list[dict] = []
    seen: set[str] = set()
    pages_checked = 0
    cursor = ""

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
            profile: dict[str, Any] = _profile_from_graphql(session, user_id)
            profile.setdefault("profile_id", user_id)
            profile.setdefault("user_id", user_id)
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
                older_than_today = False
                for feed in feeds:
                    if not isinstance(feed, dict):
                        continue
                    photo = feed.get("photo") or {}
                    author = feed.get("author") or {}
                    video_id = str(photo.get("id") or "").strip()
                    if not video_id or video_id in seen:
                        continue
                    seen.add(video_id)
                    if not profile.get("display_name"):
                        profile["display_name"] = author.get("name")
                    if not profile.get("avatar_url"):
                        profile["avatar_url"] = author.get("headerUrl") or author.get("avatar")
                    published_at = _timestamp_ms(photo.get("timestamp") or photo.get("time"))
                    if published_at is None:
                        continue
                    if since_timestamp is not None and published_at < since_timestamp:
                        older_than_today = True
                        continue
                    videos.append({
                        "id": video_id,
                        "url": "https://www.kuaishou.com/short-video/" + video_id,
                        "title": photo.get("caption") or photo.get("title") or "",
                        "description": photo.get("caption") or "",
                        "published_at": published_at,
                    })

                next_cursor = str(payload.get("pcursor") or "")
                if older_than_today or not feeds or not next_cursor or next_cursor == cursor:
                    cursor = ""
                    break
                cursor = next_cursor
                if page_number + 1 < max_pages:
                    time.sleep(1.2)
        finally:
            context.close()
            browser.close()

    if not profile.get("display_name"):
        raise HTTPException(
            status_code=404,
            detail="Không xác minh được hồ sơ Kuaishou. Kiểm tra liên kết trang cá nhân và phiên đăng nhập."
        )

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
        return _profile_scan(user_id, request.cookie, request.max_pages, request.since_timestamp)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Kuaishou profile scan failed")
        message = str(exc).strip()[:300] or type(exc).__name__
        raise HTTPException(status_code=502, detail=f"Không quét được hồ sơ Kuaishou: {message}")
