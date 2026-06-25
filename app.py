import os
import re
import urllib.parse
import json
import logging
import requests
import time
import hashlib
import subprocess
import threading
import ipaddress
import socket
from flask import Flask, request, jsonify, render_template, Response, send_file
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, template_folder='templates')
PREVIEW_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".preview_cache")
PREVIEW_LOCKS = {}
PREVIEW_LOCKS_GUARD = threading.Lock()
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "9880"))
APP_DEBUG = os.environ.get("APP_DEBUG", "").lower() in ("1", "true", "yes")
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "20"))

MEDIA_DOMAIN_SUFFIXES = (
    "douyin.com",
    "douyinpic.com",
    "douyinstatic.com",
    "douyinvod.com",
    "snssdk.com",
    "365yg.com",
    "xhscdn.com",
    "xiaohongshu.com",
    "kuaishou.com",
    "gifshow.com",
    "chenzhongtech.com",
    "kwaicdn.com",
    "kwimgs.com",
    "yximgs.com",
    "wskwai.com",
    "wsukwai.com",
)
SYNTHETIC_PROXY_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)

# Default user agent for requests (mobile safari)
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"

def extract_url(text):
    """Extract first HTTP/HTTPS URL from text."""
    match = re.search(r'https?://[^\s]+', text)
    return match.group(0) if match else None

def normalize_media_url(url):
    """Normalize protocol-relative media URLs without changing signed query strings."""
    if not isinstance(url, str):
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url

def is_public_media_url(url):
    """Allow proxying only public HTTP(S) media hosts used by supported platforms."""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("http", "https") or not hostname:
            return False
        if not any(
            hostname == suffix or hostname.endswith("." + suffix)
            for suffix in MEDIA_DOMAIN_SUFFIXES
        ):
            return False

        # Protect the public proxy if a permitted hostname is ever poisoned to
        # loopback/private infrastructure.
        for info in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(info[4][0])
            if any(address in network for network in SYNTHETIC_PROXY_NETWORKS):
                continue
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
            ):
                return False
        return True
    except (ValueError, OSError):
        return False

def get_media_response(url, headers, max_redirects=5):
    """Fetch media while validating every redirect target."""
    current_url = url
    for _ in range(max_redirects + 1):
        if not is_public_media_url(current_url):
            raise ValueError("Unsupported or unsafe media URL")
        response = requests.get(
            current_url,
            headers=headers,
            stream=True,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ValueError("Media redirect is missing a destination")
        current_url = urllib.parse.urljoin(current_url, location)
    raise ValueError("Too many media redirects")

def proxy_url(url, endpoint="/api/video_proxy"):
    """Wrap one remote media URL with a local endpoint."""
    if not url or not url.startswith(("http://", "https://")):
        return url or ""
    return f"{endpoint}?url={urllib.parse.quote(url)}"

def proxy_url_list(urls):
    """Wrap a media URL list while preserving empty Live Photo slots."""
    return [proxy_url(url) if url else "" for url in (urls or [])]

def parse_supported_url(url):
    """Dispatch a supported share URL to its platform parser."""
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    parsers = (
        (("douyin.com", "iesdouyin.com"), parse_douyin),
        (("kuaishou.com", "gifshow.com", "chenzhongtech.com"), parse_kuaishou),
        (("xiaohongshu.com", "xhslink.com"), parse_xiaohongshu),
    )
    for domains, parser in parsers:
        if any(hostname == domain or hostname.endswith("." + domain) for domain in domains):
            return parser(url)
    raise ValueError("不支持该平台链接，目前仅支持抖音、快手、小红书。")

def first_url(value):
    """Return the first URL from the address shapes used by Douyin/XHS."""
    if isinstance(value, str):
        return normalize_media_url(value)
    if isinstance(value, list):
        for item in value:
            url = first_url(item)
            if url:
                return url
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "download_url_list", "masterUrl",
                    "master_url", "backupUrls", "backup_urls", "url", "src"):
            url = first_url(value.get(key))
            if url:
                return url
    return ""

def extract_motion_video(media):
    """Extract the video paired with one image, never the post's music track."""
    if not isinstance(media, dict):
        return ""

    # Platforms have used all of these names for the per-image motion resource.
    for key in ("video", "live_photo", "livePhoto", "dynamic_video",
                "dynamicVideo", "video_info", "videoInfo"):
        value = media.get(key)
        if not isinstance(value, dict):
            continue
        for address_key in ("play_addr", "play_addr_h264", "playAddr",
                            "download_addr", "downloadAddr", "url_list"):
            url = first_url(value.get(address_key))
            if url:
                return url
        url = extract_motion_video(value)
        if url:
            return url

    stream = media.get("stream")
    if isinstance(stream, dict):
        # H.264 is preferred because browsers and downloaded MP4 players support it broadly.
        for codec in ("h264", "h265", "h266", "av1"):
            url = first_url(stream.get(codec))
            if url:
                return url
    return ""

def find_browser_executable():
    """Find an installed Chromium browser for Douyin's JavaScript verification."""
    candidates = [
        os.environ.get("DOUYIN_BROWSER_PATH", ""),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), "")

def fetch_douyin_browser_detail(content_id, timeout_ms=None):
    """
    Fetch the complete public work object after Douyin's browser verification.

    Douyin's mobile share page strips images[*].video from Live Photo posts.
    The verified PC page requests the public post list, which retains the
    real video.play_addr paired with every Live image.
    """
    if timeout_ms is None:
        timeout_ms = int(os.environ.get("DOUYIN_BROWSER_TIMEOUT_MS", "45000"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logging.warning(
            "Playwright is not installed; cannot recover Douyin Live Photo streams"
        )
        return None

    executable = find_browser_executable()
    if not executable:
        logging.warning(
            "No Edge/Chrome executable found; cannot recover Douyin Live Photo streams"
        )
        return None

    matched_detail = {}
    target_url = f"https://www.douyin.com/note/{content_id}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=executable,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                locale="zh-CN",
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            def capture_public_post_list(response):
                if "/aweme/v1/web/aweme/post/" not in response.url:
                    return
                try:
                    payload = response.json()
                    for item in payload.get("aweme_list", []):
                        if str(item.get("aweme_id")) == str(content_id):
                            matched_detail.update(item)
                            break
                except Exception as exc:
                    logging.debug("Could not inspect Douyin post response: %s", exc)

            page.on("response", capture_public_post_list)
            page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)

            deadline = time.monotonic() + timeout_ms / 1000
            while not matched_detail and time.monotonic() < deadline:
                page.wait_for_timeout(250)

            context.close()
            browser.close()
    except Exception as exc:
        logging.warning("Douyin browser detail fallback failed: %s", exc)
        return None

    return matched_detail or None

def parse_douyin(url):
    """Parse Douyin video URL to extract unwatermarked video."""
    session = requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
    }
    
    # Step 1: Follow redirects to get real URL
    logging.info(f"Following redirect for Douyin URL: {url}")
    response = session.get(url, headers=headers, allow_redirects=True, timeout=10)
    real_url = response.url
    logging.info(f"Resolved real URL: {real_url}")
    
    # Extract video or note ID
    video_id_match = re.search(r'(video|note)/(\d+)', real_url)
    if not video_id_match:
        raise Exception("无法从链接中获取抖音视频或图文ID。请确保是合法的抖音分享链接。")
    
    content_kind = video_id_match.group(1)
    video_id = video_id_match.group(2)
    logging.info(f"Extracted Douyin content ID: {video_id}")
    
    # Step 2: Fetch the mobile share page
    share_url = f"https://www.iesdouyin.com/share/{content_kind}/{video_id}/"
    logging.info(f"Fetching mobile share page: {share_url}")
    
    share_response = session.get(share_url, headers=headers, timeout=10)
    if share_response.status_code != 200:
        raise Exception(f"请求抖音分享页面失败，状态码: {share_response.status_code}")
        
    html_content = share_response.text
     
    # Step 3: Extract window._ROUTER_DATA
    router_data_match = re.search(r'window\._ROUTER_DATA\s*=\s*(.*?);?\s*</script>', html_content)
    if not router_data_match:
        router_data_match = re.search(r'window\._ROUTER_DATA\s*=\s*(.*?);', html_content)
        
    if not router_data_match:
        raise Exception("无法从抖音分享页面中解析到路由数据(可能触发验证)")
        
    try:
        raw_json = router_data_match.group(1).strip()
        if raw_json.endswith(";"):
            raw_json = raw_json[:-1]
        router_data = json.loads(raw_json)
        loader_data = router_data.get("loaderData", {})
        page_data = (
            loader_data.get("note_(id)/page")
            or loader_data.get("video_(id)/page")
            or {}
        )
        video_info_res = page_data.get("videoInfoRes", {})
        item_list = video_info_res.get("item_list", [])
        if not item_list:
            filter_list = video_info_res.get("filter_list", [])
            filter_reason = filter_list[0].get("filter_reason") if filter_list else "未知原因"
            raise Exception(f"抖音接口未返回内容详情(原因: {filter_reason})")
        detail = item_list[0]
    except Exception as e:
        logging.error(f"Failed to parse _ROUTER_DATA: {e}")
        raise Exception(f"解析抖音页面数据失败: {str(e)}")
        
    # The share page strips the video paired with each Live image. Ask the
    # verified public PC page for the complete work object when that happens.
    share_images = detail.get("images") or detail.get("image_list") or []
    if share_images and not any(extract_motion_video(item) for item in share_images):
        browser_detail = fetch_douyin_browser_detail(video_id)
        if browser_detail:
            detail = browser_detail

    # Extract video info
    title = detail.get('desc', f"抖音内容_{video_id}")
    
    # Author nickname
    author = detail.get('author', {}).get('nickname', '抖音用户')
    
    # Cover image
    cover = ""
    cover_urls = detail.get('video', {}).get('cover', {}).get('url_list', [])
    if cover_urls:
        cover = cover_urls[0]
        
    # Play URL (without watermark) or Image list (图集)
    video_url = ""
    images = []
    live_photos = []
    media_type = "video"
    
    # Check for image collection (图集) first
    image_list_obj = detail.get('images') or detail.get('image_list')
    if image_list_obj:
        media_type = "images"
        for img_obj in image_list_obj:
            image_url = first_url(
                img_obj.get("url_list")
                or img_obj.get("download_url_list")
                or img_obj.get("url")
            )
            if image_url:
                images.append(image_url)
            
            # Extract Douyin Live Photo stream if exists
            live_photos.append(extract_motion_video(img_obj))
    else:
        # Try different play address fields for video
        video_obj = detail.get('video', {})
        play_addr_obj = video_obj.get('play_addr') or video_obj.get('play_addr_h264')
        if play_addr_obj and play_addr_obj.get('url_list'):
            video_url = play_addr_obj['url_list'][0]
            
        if not video_url:
            raise Exception("无法提取无水印视频播放直链接")
            
        # Replace playwm with play in case it is watermarked (sometimes older videos have playwm)
        if 'playwm' in video_url:
            video_url = video_url.replace('playwm', 'play')
            
        # Ensure secure URL
        if video_url.startswith('//'):
            video_url = 'https:' + video_url
            
    if cover.startswith('//'):
        cover = 'https:' + cover
        
    return {
        "title": title,
        "desc": title,
        "cover": cover,
        "url": video_url if media_type == "video" else "",
        "images": images,
        "live_photos": live_photos,
        "author": author,
        "media_type": media_type,
        "platform": "抖音"
    }

def parse_kuaishou(url):
    """Parse Kuaishou video URL to extract unwatermarked video."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.kuaishou.com/"
    }
    
    # Step 1: Follow redirects
    logging.info(f"Following redirect for Kuaishou URL: {url}")
    response = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
    real_url = response.url
    html_content = response.text
    logging.info(f"Resolved real Kuaishou URL: {real_url}")
    
    # Current mobile share pages expose the full work in window.INIT_STATE.
    init_state_match = re.search(
        r'window\.INIT_STATE\s*=\s*({.*?})\s*</script>',
        html_content,
        re.DOTALL,
    )

    if init_state_match:
        try:
            init_state = json.loads(init_state_match.group(1))

            def find_photo(value):
                if isinstance(value, dict):
                    photo = value.get("photo")
                    if isinstance(photo, dict) and (
                        photo.get("photoId") or photo.get("mainMvUrls")
                    ):
                        return photo
                    for child in value.values():
                        result = find_photo(child)
                        if result:
                            return result
                elif isinstance(value, list):
                    for child in value:
                        result = find_photo(child)
                        if result:
                            return result
                return None

            photo = find_photo(init_state)
            if photo:
                title = photo.get("caption") or "快手视频"
                author = photo.get("userName") or "快手用户"
                cover = first_url(
                    photo.get("coverUrls")
                    or photo.get("webpCoverUrls")
                    or photo.get("coverUrl")
                )

                # Kuaishou atlas posts keep image paths under
                # ext_params.atlas.list and CDN hostnames separately.
                atlas = (photo.get("ext_params") or {}).get("atlas") or {}
                atlas_paths = atlas.get("list") or []
                atlas_cdns = atlas.get("cdn") or []
                atlas_images = []
                if atlas_paths and atlas_cdns:
                    primary_cdn = str(atlas_cdns[0]).strip()
                    if primary_cdn and not primary_cdn.startswith(("http://", "https://")):
                        primary_cdn = f"https://{primary_cdn}"
                    for path in atlas_paths:
                        if not isinstance(path, str) or not path:
                            continue
                        atlas_images.append(
                            path if path.startswith(("http://", "https://"))
                            else f"{primary_cdn.rstrip('/')}/{path.lstrip('/')}"
                        )

                if atlas_images:
                    return {
                        "title": title,
                        "desc": title,
                        "cover": atlas_images[0],
                        "url": "",
                        "preview_url": "",
                        "images": atlas_images,
                        "live_photos": [""] * len(atlas_images),
                        "author": author,
                        "media_type": "images",
                        "platform": "快手",
                    }

                representations = []
                manifest = photo.get("manifest") or {}
                for adaptation in manifest.get("adaptationSet") or []:
                    representations.extend(adaptation.get("representation") or [])

                # Prefer AVC/H.264 so the extracted URL plays in ordinary
                # browsers. Within that codec choose the highest-quality stream.
                playable = [
                    item for item in representations
                    if isinstance(item, dict) and first_url(item.get("url"))
                ]
                avc_streams = [
                    item for item in playable
                    if str(item.get("videoCodec", "")).lower() in ("avc", "h264")
                ]
                candidates = avc_streams or playable
                if candidates:
                    best = max(
                        candidates,
                        key=lambda item: (
                            int(item.get("width") or 0) * int(item.get("height") or 0),
                            int(item.get("avgBitrate") or item.get("maxBitrate") or 0),
                        ),
                    )
                    video_url = first_url(best.get("url"))
                    if not video_url:
                        video_url = first_url(best.get("backupUrl"))
                else:
                    video_url = first_url(photo.get("mainMvUrls"))

                if video_url:
                    return {
                        "title": title,
                        "desc": title,
                        "cover": cover,
                        "url": video_url,
                        "preview_url": video_url,
                        "images": [],
                        "live_photos": [],
                        "author": author,
                        "media_type": "video",
                        "platform": "快手",
                    }
        except Exception as exc:
            logging.warning("Error parsing Kuaishou window.INIT_STATE: %s", exc)

    # Legacy pages: try parsing window.pageData.
    page_data_match = re.search(r'window\.pageData\s*=\s*({.*?});?', html_content)
    
    title = "快手短视频"
    cover = ""
    video_url = ""
    author = "快手用户"
    
    if page_data_match:
        try:
            data = json.loads(page_data_match.group(1))
            video_info = data.get("video", {})
            title = video_info.get("caption", "快手短视频")
            cover = video_info.get("poster", "")
            video_url = video_info.get("srcNoMark", video_info.get("src", ""))
            author = data.get("user", {}).get("name", "快手用户")
        except Exception as e:
            logging.warning(f"Error parsing Kuaishou window.pageData: {e}")
            
    # Fallback regex if window.pageData is not found
    if not video_url:
        # Match srcNoMark or playUrl in HTML scripts
        src_match = re.search(r'"srcNoMark"\s*:\s*"(.*?)"', html_content)
        if not src_match:
            src_match = re.search(r'"src"\s*:\s*"(.*?)"', html_content)
            
        if src_match:
            video_url = src_match.group(1).encode().decode('unicode-escape')
            
        poster_match = re.search(r'"poster"\s*:\s*"(.*?)"', html_content)
        if poster_match:
            cover = poster_match.group(1).encode().decode('unicode-escape')
            
        caption_match = re.search(r'"caption"\s*:\s*"(.*?)"', html_content)
        if caption_match:
            title = caption_match.group(1).encode().decode('unicode-escape')

    if video_url:
        return {
            "title": title,
            "desc": title,
            "cover": cover,
            "url": video_url,
            "images": [],
            "live_photos": [],
            "author": author,
            "platform": "快手"
        }
        
    raise Exception("Could not find video URL in Kuaishou response HTML.")

def clean_xhs_image_url(url, trace_id=None):
    """Bypass Xiaohongshu image watermark using traceId."""
    if not url:
        return ""
    if not trace_id:
        match = re.search(r'(1040g[a-zA-Z0-9]+)', url)
        if match:
            trace_id = match.group(1)
    if trace_id:
        return f"https://sns-img-hw.xhscdn.com/{trace_id}?imageView2/2/format/jpg"
    return url

def parse_xiaohongshu(url):
    """Parse Xiaohongshu sharing URL."""
    headers = {
        # XHS currently sends desktop requests for app share links to a
        # synthetic 404 page (-510001), while its mobile share page still
        # contains the real note and image list.
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
    }
    
    logging.info(f"Following redirect for Xiaohongshu URL: {url}")
    response = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
    real_url = response.url
    html_content = response.text
    logging.info(f"Resolved real Xiaohongshu URL: {real_url}")
    
    # Try the script-terminated regex first as it is more precise for multi-line JSON
    state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>', html_content)
    if not state_match:
        state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});?', html_content)
    
    title = "小红书分享"
    desc = ""
    cover = ""
    video_url = ""
    preview_url = ""
    author = "小红书用户"
    media_type = "image"
    images = []
    live_photos = []
    
    if state_match:
        try:
            state_str = state_match.group(1)
            # Replace undefined with null so that json.loads doesn't fail
            cleaned_state_str = re.sub(r':\s*undefined', ': null', state_str)
            data = json.loads(cleaned_state_str)
            
            note = {}
            # Layout 1: noteDetailMap
            note_detail_map = data.get("note", {}).get("noteDetailMap", {})
            if note_detail_map:
                real_note_id_match = re.search(r'(?:item|explore)/([0-9a-f]+)', real_url)
                real_note_id = real_note_id_match.group(1) if real_note_id_match else ""
                if real_note_id and real_note_id in note_detail_map:
                    note = note_detail_map[real_note_id].get("note", {})
                if not note:
                    # The map commonly starts with an empty "undefined" placeholder.
                    for entry in note_detail_map.values():
                        candidate = entry.get("note", {}) if isinstance(entry, dict) else {}
                        if candidate and (candidate.get("imageList") or candidate.get("video")):
                            note = candidate
                            break
            
            # Layout 2: noteData.data.noteData
            if not note:
                note = data.get("noteData", {}).get("data", {}).get("noteData", {})
                
            if note:
                title = note.get("title", "") or note.get("desc", "小红书分享")
                desc = note.get("desc", "") or title
                note_user = note.get("user", {})
                author = (
                    note_user.get("nickname")
                    or note_user.get("nickName")
                    or note_user.get("name")
                    or "小红书用户"
                )
                
                # Check for video or images
                if note.get("type") == "video":
                    media_type = "video"
                    video_info = note.get("video", {})
                    media_streams = video_info.get("media", {}).get("stream", {})
                    h264_streams = media_streams.get("h264", [])
                    h265_streams = media_streams.get("h265", [])
                    h264_preview = first_url(h264_streams[0]) if h264_streams else ""
                    
                    # Try to parse mediaV2 for high resolution streams
                    media_v2_str = video_info.get("mediaV2", "")
                    hd_screencast = ""
                    default_screencast = ""
                    if media_v2_str:
                        try:
                            media_v2_data = json.loads(media_v2_str)
                            opaque1 = media_v2_data.get("video", {}).get("opaque1", {})
                            hd_screencast = opaque1.get("hd_screencast_stream", "")
                            default_screencast = opaque1.get("default_screencast_stream", "")
                        except Exception as ex:
                            logging.warning(f"Error parsing mediaV2: {ex}")
                    
                    # Unwatermarked original video (H.265/HEVC, perfect for downloads)
                    origin_key = video_info.get("consumer", {}).get("originVideoKey", "")
                    if origin_key:
                        video_url = f"https://sns-video-bd.xhscdn.com/{origin_key}"
                    elif hd_screencast:
                        video_url = hd_screencast
                    else:
                        # Try H.265 stream if available, which is also watermark-free
                        if h265_streams and h265_streams[0].get("masterUrl"):
                            video_url = h265_streams[0].get("masterUrl")
                        else:
                            if h264_streams and h264_streams[0].get("masterUrl"):
                                video_url = h264_streams[0].get("masterUrl")
                            
                    # Preview with H.264 immediately; keep the original clean
                    # source for downloading.
                    preview_url = (
                        hd_screencast
                        or default_screencast
                        or h264_preview
                        or video_url
                    )
                                
                    cover = clean_xhs_image_url(video_info.get("cover", {}).get("url", ""))
                else:
                    media_type = "images"
                    image_list = note.get("imageList", [])
                    for img in image_list:
                        img_trace = img.get("traceId") or img.get("fileId")
                        img_url = img.get("urlDefault", img.get("url", ""))
                        images.append(clean_xhs_image_url(img_url, img_trace))
                        
                        # Some XHS page variants omit the livePhoto boolean while
                        # still exposing a per-image motion stream.
                        live_video_url = extract_motion_video(img)
                        live_photos.append(live_video_url)
                    if images:
                        cover = images[0]
        except Exception as e:
            logging.warning(f"Error parsing XHS window.__INITIAL_STATE__: {e}")
            
    # Fallback to regex matches if INITIAL_STATE failed
    if not video_url and not images:
        origin_key_match = re.search(r'"originVideoKey"\s*:\s*"(.*?)"', html_content)
        if origin_key_match:
            try:
                origin_key = origin_key_match.group(1).encode().decode('unicode-escape')
                video_url = f"https://sns-video-bd.xhscdn.com/{origin_key}"
                media_type = "video"
            except Exception as e:
                logging.warning(f"Error decoding fallback originVideoKey: {e}")
                
        if not video_url:
            # Match image/video URLs inside meta tags or script
            video_match = re.search(r'"masterUrl"\s*:\s*"(.*?)"', html_content)
            if video_match:
                video_url = video_match.group(1).encode().decode('unicode-escape')
                media_type = "video"
            
        # Match title
        title_match = re.search(r'<meta property="og:title" content="(.*?)"', html_content)
        if title_match:
            title = title_match.group(1)
            desc = title
            
        # Match image list
        image_matches = re.findall(r'"urlDefault"\s*:\s*"(.*?)"', html_content)
        if image_matches:
            images = [clean_xhs_image_url(img.encode().decode('unicode-escape')) for img in image_matches]
            # Remove duplicates while keeping order
            seen = set()
            images = [x for x in images if not (x in seen or seen.add(x))]
            cover = images[0] if images else ""
            if media_type != "video":
                media_type = "images"
                live_photos = [""] * len(images)
                
    if video_url or images:
        if video_url:
            if video_url.startswith('//'):
                video_url = 'https:' + video_url
            elif video_url.startswith('http://'):
                video_url = 'https://' + video_url[7:]
        if preview_url:
            if preview_url.startswith('//'):
                preview_url = 'https:' + preview_url
            elif preview_url.startswith('http://'):
                preview_url = 'https://' + preview_url[7:]
                
        if media_type == "images" and not live_photos:
            live_photos = [""] * len(images)
        if not desc:
            desc = title
            
        return {
            "title": title,
            "desc": desc,
            "cover": cover,
            "url": video_url if media_type == "video" else "",
            "preview_url": preview_url if media_type == "video" else "",
            "images": images,
            "live_photos": live_photos,
            "author": author,
            "media_type": media_type,
            "platform": "小红书"
        }
        
    raise Exception("Could not find video or image assets in Xiaohongshu HTML.")

def get_preview_lock(cache_key):
    with PREVIEW_LOCKS_GUARD:
        return PREVIEW_LOCKS.setdefault(cache_key, threading.Lock())

def transcode_xhs_preview(source_url, output_path):
    """Convert an original XHS HEVC video to browser-compatible H.264."""
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(f"未找到可用的视频转码组件: {exc}") from exc

    temp_path = output_path + ".tmp.mp4"
    headers = (
        f"User-Agent: {USER_AGENT}\r\n"
        "Referer: https://www.xiaohongshu.com/\r\n"
    )
    command = [
        ffmpeg_exe,
        "-y",
        "-headers", headers,
        "-i", source_url,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        temp_path,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace")[-1200:]
            raise RuntimeError(f"视频预览转码失败: {error}")
        os.replace(temp_path, output_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@app.route('/api/xhs_video_preview', methods=['GET', 'OPTIONS'])
def xhs_video_preview():
    """Serve a cached H.264 preview generated from the original clean XHS video."""
    if request.method == 'OPTIONS':
        return Response(status=200)

    source_url = request.args.get('url', '').strip()
    if not is_public_media_url(source_url):
        return "Invalid Xiaohongshu video URL", 400

    os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    output_path = os.path.join(PREVIEW_CACHE_DIR, f"{cache_key}.mp4")

    lock = get_preview_lock(cache_key)
    try:
        with lock:
            if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
                transcode_xhs_preview(source_url, output_path)
        return send_file(
            output_path,
            mimetype="video/mp4",
            conditional=True,
            download_name="preview.mp4",
        )
    except Exception as exc:
        logging.exception("XHS preview generation failed")
        return f"Preview failed: {exc}", 500

@app.after_request
def after_request(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST,DELETE,OPTIONS'
    return response

@app.route('/api/video_proxy', methods=['GET', 'OPTIONS'])
def video_proxy():
    """Proxy video streams to bypass hotlink protection (Referer check)."""
    if request.method == 'OPTIONS':
        return Response(status=200, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Allow-Methods': '*'
        })
        
    url = request.args.get('url')
    if not url:
        return "Missing url parameter", 400
    if not is_public_media_url(url):
        return "Unsupported or unsafe media URL", 400
        
    range_header = request.headers.get('Range')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    }
    # Set referers based on platform domains to bypass hotlink blockages (403 Forbidden)
    if "douyin" in url or "365yg.com" in url or "snssdk.com" in url:
        headers['Referer'] = 'https://www.douyin.com/'
    elif any(domain in url for domain in (
        "kuaishou", "gifshow", "chenzhongtech", "kwaicdn",
        "kwimgs", "yximgs",
    )):
        headers['Referer'] = 'https://www.kuaishou.com/'
    elif "xiaohongshu" in url or "xhscdn" in url:
        headers['Referer'] = 'https://www.xiaohongshu.com/'
        
    if range_header:
        headers['Range'] = range_header
        
    try:
        resp = get_media_response(url, headers)
        
        # Build proxy response headers
        proxy_headers = {}
        for h in ['Content-Type', 'Content-Range', 'Content-Length', 'Content-Disposition']:
            val = resp.headers.get(h)
            if val:
                proxy_headers[h] = val
        proxy_headers['Accept-Ranges'] = 'bytes'
        
        # Enable CORS for direct web player access
        proxy_headers['Access-Control-Allow-Origin'] = '*'
        
        # Force download header if requested
        is_download = request.args.get('download') == '1'
        if is_download:
            filename = re.sub(
                r'[^a-zA-Z0-9._-]+',
                '_',
                request.args.get('filename', 'video.mp4'),
            ).strip("._") or "video.mp4"
            proxy_headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            
        response = Response(
            resp.iter_content(chunk_size=8192),
            status=resp.status_code,
            headers=proxy_headers
        )
        response.call_on_close(resp.close)
        return response
    except Exception as e:
        return f"Proxy failed: {e}", 500

@app.route('/')
def index():
    """Render index page."""
    return render_template('index.html')

@app.route('/api/health', methods=['GET'])
def health():
    """Lightweight health check for deployment platforms and monitoring."""
    return jsonify({
        "success": True,
        "service": "media-extractor",
        "platforms": ["douyin", "kuaishou", "xiaohongshu"],
    })

@app.route('/api/extract', methods=['POST', 'OPTIONS'])
def extract():
    """Extract media endpoint."""
    if request.method == 'OPTIONS':
        return jsonify({"success": True}), 200
    try:
        data = request.json or {}
        input_text = data.get('url', '').strip()
        
        if not input_text:
            return jsonify({
                "success": False,
                "error": "请输入有效的链接地址"
            }), 400
            
        # Extract url from text
        url = extract_url(input_text)
        if not url:
            return jsonify({
                "success": False,
                "error": "未在输入中检测到有效的HTTP/HTTPS链接"
            }), 400
            
        try:
            parsed_data = parse_supported_url(url)
        except Exception as parse_err:
            logging.error(f"Parsing failed for {url}: {parse_err}")
            return jsonify({
                "success": False,
                "error": f"解析失败: {parse_err}"
            }), 400
            
        original_url = parsed_data.get("url")
        original_preview_url = parsed_data.get("preview_url")
        parsed_data["url"] = proxy_url(original_url)
        preview_endpoint = (
            "/api/xhs_video_preview"
            if (
                parsed_data.get("platform") == "小红书"
                and original_preview_url
                and original_preview_url == original_url
            )
            else "/api/video_proxy"
        )
        parsed_data["preview_url"] = proxy_url(
            original_preview_url,
            preview_endpoint,
        )
        parsed_data["images"] = proxy_url_list(parsed_data.get("images"))
        parsed_data["live_photos"] = proxy_url_list(parsed_data.get("live_photos"))

        # Return success response
        return jsonify({
            "success": True,
            "data": parsed_data
        })
        
    except Exception as general_err:
        logging.error(f"General extraction error: {general_err}")
        return jsonify({
            "success": False,
            "error": f"系统解析出错: {str(general_err)}"
        }), 500

if __name__ == '__main__':
    # Ensure templates folder exists
    os.makedirs('templates', exist_ok=True)
    
    
    # Run server
    print("\n" + "="*50)
    print(" 去水印视频提取工具已成功启动！")
    print(" 请在浏览器中访问: http://127.0.0.1:9880")
    print("="*50 + "\n")
    
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG)
