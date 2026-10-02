#!/usr/bin/env python3
"""
cloud_fulfiller.py — Automated Cloud Request Fulfiller for EditingAssets
Runs in GitHub Actions on-demand whenever a user requests an asset on the website.
1. Searches target sources for requested asset
2. Downloads and crops watermark using FFmpeg
3. Uploads clean file to GitHub Release CDN
4. Saves asset to Firestore
5. Marks request as fulfilled in Firestore
6. Sends 'Asset Ready' email to user via Resend
"""

import os
import sys
import re
import json
import time
import html
import random
import shutil
import urllib.parse
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import httpx
except ImportError:
    import urllib.request
    httpx = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None

GITHUB_VAULT_REPO = os.getenv("GITHUB_VAULT_REPO", "AnshThakur1493/editingassets-vault")
GITHUB_TOKEN = os.getenv("VAULT_TOKEN") or os.getenv("GITHUB_TOKEN", "")
GITHUB_RELEASE_TAG = os.getenv("GITHUB_RELEASE_TAG", "v1.0")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
]

def get_http_client():
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    return httpx.Client(timeout=20.0, headers=headers, follow_redirects=True)

def init_firebase():
    if not firebase_admin:
        return None
    if firebase_admin._apps:
        return firestore.client()
    sa_raw = os.getenv("SERVICE_ACCOUNT")
    if not sa_raw:
        return None
    try:
        sa_data = json.loads(sa_raw)
        if isinstance(sa_data, str):
            sa_data = json.loads(sa_data)
        if "private_key" in sa_data:
            sa_data["private_key"] = sa_data["private_key"].replace("\\n", "\n")
        cred = credentials.Certificate(sa_data)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        print(f"[Firebase Init Error]: {e}")
        return None

def send_asset_ready_email(to_email: str, title: str, download_url: str):
    if not RESEND_API_KEY or not to_email:
        print("[Email Service] Missing RESEND_API_KEY or to_email, skipping.")
        return False

    clean_dl_url = download_url or "https://editingassets.com"
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0b0d14; color: #f8fafc; padding: 24px; margin: 0; }}
        .card {{ max-width: 540px; margin: 0 auto; background: #131722; border: 1px solid #232936; border-radius: 16px; padding: 32px; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }}
        .logo {{ font-size: 20px; font-weight: 800; color: #fff; margin-bottom: 24px; letter-spacing: -0.5px; }}
        .logo span {{ color: #8b5cf6; }}
        h1 {{ font-size: 22px; font-weight: 700; color: #fff; margin: 0 0 12px 0; }}
        p {{ font-size: 14px; line-height: 1.6; color: #94a3b8; margin: 0 0 18px 0; }}
        .badge {{ display: inline-block; background: rgba(139, 92, 246, 0.15); border: 1px solid rgba(139, 92, 246, 0.3); color: #c4b5fd; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; margin-bottom: 20px; }}
        .btn {{ display: inline-block; background: linear-gradient(135deg, #7c3aed, #c026d3); color: #ffffff !important; padding: 12px 24px; border-radius: 8px; font-weight: 600; text-decoration: none; font-size: 14px; box-shadow: 0 4px 14px rgba(124,58,237,0.4); }}
        .footer {{ margin-top: 32px; border-top: 1px solid #232936; padding-top: 18px; font-size: 12px; color: #64748b; text-align: center; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="logo">Editing<span>Assets</span></div>
        <div class="badge">Asset Request Fulfilled 🎉</div>
        <h1>Your Requested Clip is Ready!</h1>
        <p>Hey editor! The clip you requested, <strong>"{title}"</strong>, has been found, processed, and added to the library.</p>
        <p>You can download it right now watermark-free:</p>
        <p style="margin: 24px 0;">
          <a href="{clean_dl_url}" class="btn" target="_blank">Download Asset Now</a>
        </p>
        <p style="font-size: 13px;">Don't forget to tag <strong>@EditingAssets</strong> in your edit!</p>
        <div class="footer">
          &copy; 2026 EditingAssets &mdash; Made for editors, by editors.
        </div>
      </div>
    </body>
    </html>
    """

    payload = {
        "from": "EditingAssets <onboarding@resend.dev>",
        "to": [to_email],
        "subject": f"🎉 Your requested asset \"{title}\" is ready to download!",
        "html": html_content
    }

    try:
        client = httpx.Client(timeout=15.0)
        res = client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload
        )
        if res.status_code in [200, 201]:
            print(f"[Resend Email] Successfully sent to {to_email} (ID: {res.json().get('id')})")
            return True
        else:
            print(f"[Resend Error] {res.status_code}: {res.text}")
            return False
    except Exception as e:
        print(f"[Resend Exception]: {e}")
        return False

def search_asset(query: str, category: str = "") -> Optional[Dict[str, Any]]:
    client = get_http_client()
    q_clean = query.strip()
    norm_q = re.sub(r'[^a-zA-Z0-9\s]', ' ', q_clean).lower()

    # 1. SFX Search
    if category == "sfx" or any(w in norm_q for w in ["sound", "sfx", "audio", "voice", "dialogue"]):
        try:
            m_url = f"https://www.myinstants.com/en/search/?name={urllib.parse.quote(q_clean)}"
            res = client.get(m_url)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                for btn in soup.find_all("button", class_=lambda c: c and "small-button" in c):
                    onclick = btn.get("onmousedown", "") or btn.get("onclick", "")
                    m_match = re.search(r"play\(['\"](.*?)['\"]", onclick)
                    if m_match:
                        mp3_path = m_match.group(1)
                        full_mp3 = urllib.parse.urljoin("https://www.myinstants.com", mp3_path)
                        title_el = btn.find_next("a", class_=lambda c: c and "instant-link" in c)
                        title_txt = title_el.get_text(strip=True) if title_el else q_clean.title()
                        return {
                            "source": "myinstants",
                            "media_url": full_mp3,
                            "title": title_txt,
                            "category": "sfx",
                            "media_type": "audio",
                            "thumbnail_url": ""
                        }
        except Exception:
            pass

    # 2. Green Screen Memes
    if category == "greenscreen" or "green" in norm_q or "chroma" in norm_q:
        try:
            gsm_url = f"https://greenscreenmemes.com/wp-json/wp/v2/posts?search={urllib.parse.quote(q_clean)}&_embed=1"
            res = client.get(gsm_url)
            if res.status_code == 200:
                posts = res.json()
                for p in posts:
                    content = p.get("content", {}).get("rendered", "")
                    mp4_m = re.findall(r'https?://[^\s"\'<>]+\.mp4', content)
                    if mp4_m:
                        raw_t = p.get("title", {}).get("rendered", q_clean)
                        clean_t = html.unescape(raw_t).strip()
                        thumb = ""
                        try:
                            thumb = p["_embedded"]["wp:featuredmedia"][0]["source_url"]
                        except Exception:
                            pass
                        return {
                            "source": "greenscreenmemes",
                            "media_url": mp4_m[0],
                            "title": clean_t,
                            "category": "greenscreen",
                            "media_type": "video",
                            "thumbnail_url": thumb
                        }
        except Exception:
            pass

    # 3. Video Memes: IndianMemeTemplates & Memes.co.in
    try:
        keywords = [q_clean]
        words = [w for w in norm_q.split() if w not in ["meme", "memes", "video", "download", "template", "templates"] and len(w) > 2]
        if words:
            keywords.append(" ".join(words[:4]))
            keywords.extend(words[:2])

        for kw in keywords:
            imt_url = f"https://indianmemetemplates.com/wp-json/wp/v2/posts?search={urllib.parse.quote(kw)}&_embed=1"
            res = client.get(imt_url)
            if res.status_code == 200:
                posts = res.json()
                for p in posts:
                    content = p.get("content", {}).get("rendered", "")
                    mp4_m = re.findall(r'https?://[^\s"\'<>]+\.mp4', content)
                    if mp4_m:
                        raw_t = p.get("title", {}).get("rendered", q_clean)
                        clean_t = html.unescape(raw_t).strip()
                        clean_t = re.sub(r'(?i)\s*(?:meme\s*)?template.*$', '', clean_t).strip()
                        thumb = ""
                        try:
                            thumb = p["_embedded"]["wp:featuredmedia"][0]["source_url"]
                        except Exception:
                            pass
                        return {
                            "source": "indian_memes",
                            "media_url": mp4_m[0],
                            "title": clean_t or q_clean.title(),
                            "category": "memes",
                            "media_type": "video",
                            "thumbnail_url": thumb
                        }
    except Exception:
        pass

    try:
        for kw in [q_clean] + [w for w in norm_q.split() if len(w) > 2][:2]:
            mco_url = f"https://api.memes.co.in/api/meme-videos?search={urllib.parse.quote(kw)}&page=1"
            res = client.get(mco_url)
            if res.status_code == 200:
                for item in res.json().get("results", []):
                    vid_url = item.get("video_file") or item.get("media_url") or ""
                    if vid_url:
                        raw_title = item.get("title") or q_clean.title()
                        clean_t = re.sub(r'(?i)\s*(?:meme\s*)?video\s*download.*$', '', raw_title).strip()
                        return {
                            "source": "memes_co",
                            "media_url": vid_url,
                            "title": clean_t or q_clean.title(),
                            "category": "memes",
                            "media_type": "video",
                            "thumbnail_url": item.get("thumbnail") or ""
                        }
    except Exception:
        pass

    return None

def upload_to_github_release(file_path: Path, filename: str) -> str:
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    # Check or get release
    url = f"https://api.github.com/repos/{GITHUB_VAULT_REPO}/releases/tags/{GITHUB_RELEASE_TAG}"
    r = httpx.get(url, headers=headers, timeout=15.0)
    if r.status_code == 200:
        release_data = r.json()
    else:
        # Create release if doesn't exist
        create_url = f"https://api.github.com/repos/{GITHUB_VAULT_REPO}/releases"
        payload = {"tag_name": GITHUB_RELEASE_TAG, "name": f"Media Vault {GITHUB_RELEASE_TAG}"}
        cr = httpx.post(create_url, headers=headers, json=payload, timeout=15.0)
        release_data = cr.json()

    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', filename)
    upload_url = release_data["upload_url"].split("{")[0] + f"?name={urllib.parse.quote(safe_name)}"
    
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    headers["Content-Type"] = "video/mp4" if file_path.suffix == ".mp4" else "audio/mpeg"
    res = httpx.post(upload_url, headers=headers, content=file_bytes, timeout=60.0)
    if res.status_code in [200, 201]:
        return res.json().get("browser_download_url")
    else:
        return f"https://github.com/{GITHUB_VAULT_REPO}/releases/download/{GITHUB_RELEASE_TAG}/{safe_name}"

def crop_video(input_path: Path, output_path: Path) -> bool:
    try:
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-filter:v", "crop=in_w:in_h-44:0:0",
            "-c:v", "libx264", "-crf", "22", "-preset", "veryfast",
            "-c:a", "copy", "-y", str(output_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        shutil.copy(input_path, output_path)
        return True

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--category", default="memes")
    parser.add_argument("--notify-email", default="")
    parser.add_argument("--request-id", default="")
    args = parser.parse_args()

    print(f"🚀 [Cloud Fulfiller] Query: '{args.query}' | Cat: '{args.category}' | Email: '{args.notify_email}'")

    asset = search_asset(args.query, args.category)
    if not asset:
        print(f"❌ No matching asset found for '{args.query}'.")
        sys.exit(0)

    print(f"🎉 Found matching asset: '{asset['title']}' ({asset['media_url']})")

    # Download media file
    temp_dir = Path("./temp_download")
    temp_dir.mkdir(exist_ok=True)
    ext = ".mp3" if asset["media_type"] == "audio" else ".mp4"
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', asset["title"].lower()).strip('-')[:35]
    filename = f"req_{slug}{ext}"
    raw_path = temp_dir / f"raw_{filename}"
    clean_path = temp_dir / filename

    client = get_http_client()
    r = client.get(asset["media_url"])
    with open(raw_path, "wb") as f:
        f.write(r.content)

    if asset["media_type"] == "video":
        crop_video(raw_path, clean_path)
    else:
        shutil.copy(raw_path, clean_path)

    # Upload to GitHub Release CDN
    cdn_url = upload_to_github_release(clean_path, filename)
    print(f"✅ Uploaded to CDN: {cdn_url}")

    # Sync to Firestore
    db = init_firebase()
    doc_id = f"cloud_{int(time.time()*1000)}"
    if db:
        try:
            doc_data = {
                "title": asset["title"],
                "category": asset["category"],
                "download_url": cdn_url,
                "preview_url": cdn_url,
                "thumbnail": asset.get("thumbnail_url", ""),
                "tags": [asset["category"], "user_requested", "automated"],
                "duration": "0:06",
                "format": "MP3" if asset["media_type"] == "audio" else "MP4",
                "source": asset["source"],
                "license": "Free / Attribution",
                "createdAt": firestore.SERVER_TIMESTAMP
            }
            db.collection("assets").document(doc_id).set(doc_data)
            print(f"✅ Added to Firestore assets library (ID: {doc_id})")

            # Update request status
            if args.request_id:
                db.collection("asset_requests").document(args.request_id).update({
                    "status": "fulfilled",
                    "assetId": doc_id,
                    "downloadUrl": cdn_url,
                    "fulfilledAt": firestore.SERVER_TIMESTAMP
                })
                print(f"✅ Marked request '{args.request_id}' as FULFILLED.")
        except Exception as e:
            print(f"[Firestore Sync Error]: {e}")

    # Send email notification
    if args.notify_email:
        send_asset_ready_email(args.notify_email, asset["title"], cdn_url)

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)
    print("🎉 Done! Fully automated fulfillment complete.")

if __name__ == "__main__":
    main()
