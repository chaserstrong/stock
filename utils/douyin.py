"""抖音相关工具：博主身份校验、视频 ID 解析、Playwright 抓取主页视频列表。

数据来源：
  1. iesdouyin.com/web/api/v2/user/info/ —— 免签名，可拿昵称、作品数
  2. www.douyin.com/user/{sec_uid}      —— Playwright 渲染主页，拦截 API 拿视频列表
  3. www.douyin.com/video/{aweme_id}     —— detail API 的 subtitle_infos 可拿字幕
  4. detail API 无字幕时回退：下载视频 → 提音频 → 必剪 ASR → pysrt 取正文
  5. 仍无字幕的视频回退到 Tavily extract 抓详情页正文
"""

import datetime
import os
import re

import certifi
import requests

from utils.tavily import tavily_search

USER_INFO_URL = "https://www.iesdouyin.com/web/api/v2/user/info/"

# 抖音服务在东八区，发布时间按北京时间解释
_CST = datetime.timezone(datetime.timedelta(hours=8))

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 抖音站内视频 URL 形态：/video/、/shipin/、/note/ 后面都是 aweme_id
_AWEME_URL_RE = re.compile(r"(?:video|shipin|note)/(\d{15,25})")

# 视频详情页正文里的发布时间，形如「发布时间：2025-08-07 03:37」
_PUBLISH_RE = re.compile(r"发布时间[:：]\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?")

# 视频详情页里作者头像的 alt 文本，形如「Image 28: 全能的野人」
_AUTHOR_IMG_RE = re.compile(r"Image\s*\d+\s*[:：]\s*([^)\]\n]{1,40})")

# webvtt/srt 时间轴行，形如「00:00:01.000 --> 00:00:03.000」
_VTT_TIMESTAMP_RE = re.compile(
    r"\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}"
)

# detail API 翻页之间留间隔，避免风控
_DETAIL_FETCH_INTERVAL_MS = 1500


def aweme_id_from_url(url: str) -> str | None:
    """从抖音 URL 中取出 aweme_id（视频唯一 ID）。"""
    m = _AWEME_URL_RE.search(url or "")
    return m.group(1) if m else None


def video_url(aweme_id: str) -> str:
    """拼出标准视频详情页地址。"""
    return f"https://www.douyin.com/video/{aweme_id}"


def date_from_aweme_id(aweme_id: str) -> datetime.datetime:
    """用 aweme_id 反推发布时间（高 32 位是发布时刻的 Unix 秒）。"""
    return datetime.datetime.fromtimestamp(int(aweme_id) >> 32, _CST)


def find_sec_uid(creator: str) -> str | None:
    """定位博主 sec_uid，均用 iesdouyin 接口校验昵称精准匹配。

    依次尝试：抖音站内搜索 API → 抖音搜索页渲染 → Tavily 第三方搜索。
    只有昵称精确匹配目标博主的候选才会返回；全部失败返回 None。
    """
    sec_uid = _find_sec_uid_via_douyin(creator)
    if sec_uid:
        return sec_uid
    print("  抖音站内搜索未命中，回退 Tavily 第三方搜索...")
    return _find_sec_uid_via_tavily(creator)


def _verify_sec_uid(sec_uid: str, creator: str) -> bool:
    """用 iesdouyin 免签名接口校验昵称是否精确匹配目标博主。"""
    info = fetch_user_info(sec_uid)
    return (info.get("nickname") or "").strip() == creator


def _pick_sec_uid_by_nickname(sec_uids: list, creator: str) -> str | None:
    """从候选 sec_uid 列表里挑出昵称精确匹配目标的第一个。"""
    seen = set()
    for sec_uid in sec_uids:
        if not sec_uid or sec_uid in seen:
            continue
        seen.add(sec_uid)
        if _verify_sec_uid(sec_uid, creator):
            return sec_uid
    return None


def _find_sec_uid_via_douyin(creator: str) -> str | None:
    """用 Playwright 走抖音站内搜索拿 sec_uid（搜索 API → 搜索页渲染）。"""
    from playwright.sync_api import sync_playwright

    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_chrome_data")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=data_dir,
            headless=False,
            channel="chrome",
            user_agent=_HEADERS["User-Agent"],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-crash-reporter",
            ],
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = context.new_page()

        # 先访问首页拿 cookie（ttwid 等），否则搜索接口会被风控
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        sec_uid = None
        # 1. 站内搜索 API（page.evaluate，签名由页面 JS 注入）
        try:
            sec_uid = _find_sec_uid_by_search_api(page, creator)
        except Exception as e:
            print(f"  搜索 API 失败：{e}")

        # 2. 回退：渲染搜索页，从结果卡提取候选
        if not sec_uid:
            try:
                sec_uid = _find_sec_uid_by_search_page(page, creator)
            except Exception as e:
                print(f"  搜索页渲染失败：{e}")

        context.close()
    return sec_uid


def _find_sec_uid_by_search_api(page, creator: str) -> str | None:
    """在页面上下文内调抖音搜索 API，收集候选 sec_uid 并校验昵称。"""
    resp = page.evaluate(
        """
        async (keyword) => {
            const url = '/aweme/v1/web/general/search/single/?device_platform=webapp'
                + '&aid=6383&search_channel=aweme_user'
                + '&keyword=' + encodeURIComponent(keyword)
                + '&search_source=normal_search&query_correct_type=1'
                + '&is_filter_search=0&from_page_name=search'
                + '&offset=0&count=15';
            const resp = await fetch(url, {credentials: 'include'});
            const data = await resp.json();
            const secUids = [];
            const collect = (obj) => {
                if (!obj || typeof obj !== 'object') return;
                if (typeof obj.sec_uid === 'string') secUids.push(obj.sec_uid);
                if (obj.user_info && typeof obj.user_info.sec_uid === 'string')
                    secUids.push(obj.user_info.sec_uid);
                const vals = Array.isArray(obj) ? obj : Object.values(obj);
                for (const v of vals) collect(v);
            };
            collect(data);
            return { status_code: data.status_code, secUids };
        }
        """,
        creator,
    )
    if not resp or resp.get("status_code") != 0:
        return None
    return _pick_sec_uid_by_nickname(resp.get("secUids") or [], creator)


def _find_sec_uid_by_search_page(page, creator: str) -> str | None:
    """渲染抖音搜索页(type=user)，从结果卡提取候选 sec_uid 并校验昵称。"""
    import urllib.parse

    url = f"https://www.douyin.com/search/{urllib.parse.quote(creator)}?type=user"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)
    # 滚动触发懒加载，多收集几张用户卡
    page.mouse.wheel(0, 1500)
    page.wait_for_timeout(2000)

    hrefs = page.eval_on_selector_all(
        "a[href*='/user/']",
        "els => els.map(el => el.getAttribute('href')).filter(Boolean)",
    )
    sec_uids = []
    for href in hrefs or []:
        m = re.search(r"/user/([A-Za-z0-9_\-]{20,})", href)
        if m:
            sec_uids.append(m.group(1))
    return _pick_sec_uid_by_nickname(sec_uids, creator)


def _find_sec_uid_via_tavily(creator: str) -> str | None:
    """Tavily 搜索抖音主页，取候选 sec_uid 并校验昵称（最终回退）。"""
    result = tavily_search(
        query=f"{creator} 抖音 主页",
        search_depth="advanced",
        include_domains=["douyin.com"],
        max_results=15,
        country="china",
        language="zh",
    )
    sec_uids = []
    for item in result.get("results", []):
        m = re.search(r"/user/([A-Za-z0-9_\-]{20,})", item.get("url", ""))
        if m:
            sec_uids.append(m.group(1))
    return _pick_sec_uid_by_nickname(sec_uids, creator)


def fetch_user_info(sec_uid: str) -> dict:
    """调抖音免签名接口拿博主资料，失败返回空 dict。"""
    try:
        res = requests.get(
            USER_INFO_URL,
            params={"sec_uid": sec_uid},
            headers=_HEADERS,
            timeout=15,
            verify=certifi.where(),
        )
        return res.json().get("user_info") or {}
    except Exception:
        return {}


def parse_video_page(raw_content: str) -> dict:
    """从视频详情页 markdown 正文中解析发布时间与作者。"""
    raw = raw_content or ""
    pm = _PUBLISH_RE.search(raw)
    author = None
    if pm:
        window = raw[pm.end(): pm.end() + 2000]
        am = _AUTHOR_IMG_RE.search(window)
        if am:
            author = am.group(1).strip()
    return {
        "publish_date": pm.group(1) if pm else None,
        "publish_time": pm.group(2) if pm else None,
        "author": author,
    }


def scrape_user_videos(sec_uid: str, max_count: int = 50) -> list:
    """用 Playwright 在页面上下文内 fetch 作品列表 API。

    抖音 Web API 需要签名参数（X-Bogus 等），这些由页面 JS 自动注入。
    所以不在 Python 端直接 requests，而是用 page.evaluate 调 fetch，
    让浏览器自动带上 cookie + 签名。

    Args:
        sec_uid: 博主的 sec_uid
        max_count: 最多获取的视频数

    Returns:
        [{"aweme_id", "title", "url", "is_top", "create_time", "create_date"}]
    """
    from playwright.sync_api import sync_playwright

    videos = []
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_chrome_data")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=data_dir,
            headless=True,
            channel="chrome",
            user_agent=_HEADERS["User-Agent"],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-crash-reporter",
            ],
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = context.new_page()

        # 1. 先访问首页拿 cookie（ttwid 等）
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        # 2. 打开用户主页，让页面 JS 全部加载
        page.goto(
            f"https://www.douyin.com/user/{sec_uid}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(8000)

        # 3. 在页面上下文内 fetch API，由页面 JS 注入签名
        cursor = 0
        for _ in range(5):  # 最多翻 5 页
            if len(videos) >= max_count:
                break
            resp = page.evaluate(
                """
                async ({secUid, cursor, count}) => {
                    const url = '/aweme/v1/web/aweme/post/?device_platform=webapp&aid=6383'
                        + '&sec_user_id=' + encodeURIComponent(secUid)
                        + '&count=' + count
                        + '&max_cursor=' + cursor;
                    const resp = await fetch(url, {credentials: 'include'});
                    const data = await resp.json();
                    return {
                        status_code: data.status_code,
                        aweme_list: data.aweme_list,
                        max_cursor: data.max_cursor,
                        has_more: data.has_more,
                    };
                }
                """,
                {"secUid": sec_uid, "cursor": cursor, "count": 50},
            )

            if not resp or resp.get("status_code") != 0:
                break

            for item in resp.get("aweme_list") or []:
                aweme_id = str(item.get("aweme_id", ""))
                if not aweme_id or any(v["aweme_id"] == aweme_id for v in videos):
                    continue
                create_time = item.get("create_time", 0)
                create_date = datetime.datetime.fromtimestamp(
                    int(create_time), _CST
                ).strftime("%Y-%m-%d %H:%M") if create_time else None
                videos.append({
                    "aweme_id": aweme_id,
                    "title": item.get("desc", "") or "",
                    "url": video_url(aweme_id),
                    "is_top": item.get("is_top", 0) == 1,
                    "create_time": int(create_time) if create_time else 0,
                    "create_date": create_date,
                })
                if len(videos) >= max_count:
                    break

            if not resp.get("has_more"):
                break
            cursor = resp.get("max_cursor", 0)

        context.close()

    return videos


def fetch_video_subtitles(videos: list) -> dict:
    """用 Playwright 调 detail API 取字幕 URL 并下载文本。

    detail API 需 X-Bogus 签名（页面 JS 注入），故在页面上下文内 fetch；
    字幕文件本身在公开 CDN，用 requests 下载。

    Args:
        videos: 视频列表，每项需含 aweme_id

    Returns:
        {aweme_id: 字幕纯文本}，无字幕或失败为空字符串
    """
    from playwright.sync_api import sync_playwright

    result = {v["aweme_id"]: "" for v in videos}
    if not videos:
        return result

    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_chrome_data")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=data_dir,
            headless=False,
            channel="chrome",
            user_agent=_HEADERS["User-Agent"],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-crash-reporter",
            ],
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = context.new_page()

        # 先访问首页拿 cookie（ttwid 等），否则 detail API 会被风控
        page.goto(
            "https://www.douyin.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(5000)

        for v in videos:
            aweme_id = v["aweme_id"]
            print(f"    字幕：{v['title'][:40]}...")
            result[aweme_id] = _fetch_one_subtitle(page, aweme_id)
            page.wait_for_timeout(_DETAIL_FETCH_INTERVAL_MS)

        context.close()

    return result


def _fetch_one_subtitle(page, aweme_id: str) -> str:
    """取单个视频的字幕文本。

    优先走 detail API 的字幕 URL；接口无字幕时回退到
    下载视频 → 提音频 → 必剪 ASR → pysrt 读 srt，拿到正文。
    """
    try:
        info = page.evaluate(
            """
            async (awemeId) => {
                const url = '/aweme/v1/web/aweme/detail/?device_platform=webapp'
                    + '&aid=6383&aweme_id=' + awemeId;
                const resp = await fetch(url, {credentials: 'include'});
                const data = await resp.json();
                const video = (data.aweme_detail || {}).video || {};
                const infos = video.subtitle_infos || video.caption_infos || [];
                const subUrls = infos.map(s => s.url || s.Url).filter(Boolean);
                const playUrls = (video.play_addr || {}).url_list || [];
                return { subUrls, playUrl: playUrls[0] || '' };
            }
            """,
            aweme_id,
        )
    except Exception:
        return ""

    for url in (info or {}).get("subUrls") or []:
        text = _download_subtitle_text(url)
        if text:
            return text

    # 接口无字幕：下载视频走 ASR 回退
    play_url = (info or {}).get("playUrl") or ""
    if play_url:
        print("    接口无字幕，回退下载视频 + ASR...")
        return _asr_fallback(play_url, aweme_id)
    return ""


def _download_subtitle_text(url: str) -> str:
    """下载字幕文件并提取纯文本，兼容 JSON 与 webvtt 两种格式。"""
    if url.startswith("//"):
        url = "https:" + url
    try:
        res = requests.get(
            url, headers=_HEADERS, timeout=15, verify=certifi.where()
        )
    except Exception:
        return ""

    body = res.text or ""
    if "json" in res.headers.get("content-type", "") or body.lstrip().startswith("{"):
        try:
            return _extract_subtitle_text(res.json())
        except Exception:
            return ""
    return _text_from_vtt(body)


def _extract_subtitle_text(data) -> str:
    """从字幕 JSON 提取纯文本，兼容 utterances / body / subtitles 结构。"""
    if not isinstance(data, dict):
        return ""
    for key in ("utterances", "body", "subtitles"):
        segs = data.get(key)
        if isinstance(segs, list):
            return " ".join(
                s.get("text", "") for s in segs if isinstance(s, dict)
            ).strip()
    return ""


def _text_from_vtt(body: str) -> str:
    """从 webvtt/srt 文本中剥离时间轴与序号，拼接纯文本。"""
    lines = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line == "WEBVTT":
            continue
        if _VTT_TIMESTAMP_RE.search(line) or line.isdigit():
            continue
        lines.append(line)
    return " ".join(lines)


def _asr_fallback(play_url: str, aweme_id: str) -> str:
    """接口无字幕时的回退流程：下载视频 → 取音频/字幕 → ASR → 读 srt。

    1. 下载视频到项目根目录的 temp/
    2. 有软字幕流则 ffmpeg 直接抽 srt；否则提音频走必剪 ASR
    3. 用 pysrt 把 srt 拼成纯文本，作为 content
    4. 调用完毕后清理临时文件（视频/音频/字幕）

    Args:
        play_url: 抖音 play_addr 的直链
        aweme_id: 视频 ID，用作临时文件名

    Returns:
        字幕纯文本，失败为空字符串
    """
    from src.testExectAsr import extract_audio, has_soft_subtitle
    from utils.bCutASR import BCutASR

    temp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp")
    os.makedirs(temp_dir, exist_ok=True)

    video_path = os.path.join(temp_dir, f"{aweme_id}.mp4")
    audio_path = os.path.join(temp_dir, f"{aweme_id}.wav")
    srt_path = os.path.join(temp_dir, f"{aweme_id}.srt")

    try:
        if not _download_video(play_url, video_path):
            return ""

        if has_soft_subtitle(video_path):
            if not _extract_soft_subtitle(video_path, srt_path):
                return ""
        else:
            if not extract_audio(video_path, audio_path):
                return ""
            BCutASR().run(audio_path, srt_path)

        return _text_from_srt(srt_path)
    except Exception as e:
        print(f"    ASR 回退失败：{e}")
        return ""
    finally:
        # 调用完毕后删除临时文件（无论成功失败都清理）
        for p in (video_path, audio_path, srt_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _download_video(url: str, dst_path: str) -> bool:
    """下载抖音视频到本地，成功返回 True。"""
    if url.startswith("//"):
        url = "https:" + url
    headers = dict(_HEADERS)
    headers["Referer"] = "https://www.douyin.com/"
    try:
        with requests.get(
            url, headers=headers, timeout=60,
            verify=certifi.where(), stream=True,
        ) as res:
            res.raise_for_status()
            with open(dst_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return os.path.exists(dst_path) and os.path.getsize(dst_path) > 0
    except Exception as e:
        print(f"    视频下载失败：{e}")
        return False


def _extract_soft_subtitle(video_path: str, srt_path: str) -> bool:
    """用 ffmpeg 从视频软字幕流抽出 srt。"""
    import subprocess

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-map", "0:s:0", srt_path],
            check=True, capture_output=True, timeout=120, encoding="utf-8",
        )
        return os.path.exists(srt_path) and os.path.getsize(srt_path) > 0
    except Exception as e:
        print(f"    软字幕提取失败：{e}")
        return False


def _text_from_srt(srt_path: str) -> str:
    """用 pysrt 读取 srt 文件并拼接纯文本。"""
    import pysrt

    try:
        subs = pysrt.open(srt_path, encoding="utf-8")
        return " ".join(sub.text.replace("\n", " ") for sub in subs).strip()
    except Exception as e:
        print(f"    srt 读取失败：{e}")
        return ""
