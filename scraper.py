"""雪球抓取：Playwright 过 WAF -> 拿 xq_a_token -> 调 JSON 接口；并提取/下载帖子图片。"""
import base64
import re
import time
from io import BytesIO

import requests
from PIL import Image
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

IMG_HEADERS = {"User-Agent": UA, "Referer": "https://xueqiu.com/"}


def _apply_stealth(page):
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        try { Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] }); } catch(e){}
        try { Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh'] }); } catch(e){}
    """)


def clean_text(html):
    t = re.sub(r"<[^>]+>", "", html or "")
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
           .replace("&gt;", ">").replace("&lt;", "<"))
    return t.strip()


def download_image_b64(url, max_px=1024, quality=80):
    """下载雪球图片（带 Referer 规避防盗链），压缩后返回 base64；失败返回 None。"""
    try:
        r = requests.get(url, headers=IMG_HEADERS, timeout=20)
        if r.status_code != 200 or not r.content:
            return None
        img = Image.open(BytesIO(r.content)).convert("RGB")
        img.thumbnail((max_px, max_px))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print("[scraper] 图片下载/压缩失败:", e)
        return None


def _extract_pics(po):
    pics = []
    for p in (po.get("pics") or []):
        if isinstance(p, dict):
            u = p.get("original") or p.get("middle") or p.get("small")
        elif isinstance(p, str):
            u = p
        else:
            continue
        if u:
            pics.append(u)
    return pics


def fetch_timeline(user_id, pages=2, headless=True):
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                  viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        _apply_stealth(page)
        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        ck = {c["name"]: c["value"] for c in ctx.cookies()}
        if "xq_a_token" not in ck:
            raise RuntimeError("未拿到 xq_a_token，WAF 挑战未通过（可能被限流，稍后重试或换IP）")
        for pg in range(1, pages + 1):
            api = (f"https://xueqiu.com/statuses/user_timeline.json?"
                   f"user_id={user_id}&page={pg}&num=20&type=status&method=forUM")
            r = ctx.request.get(api, headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://xueqiu.com/u/{user_id}",
                "Accept": "application/json"})
            if r.status != 200:
                break
            data = r.json()
            out.extend(data.get("statuses") or [])
        browser.close()
    return out


def normalize(posts):
    res = []
    for po in posts:
        res.append({
            "id": po.get("id"),
            "created_at": po.get("created_at"),
            "text": clean_text(po.get("text", "")),
            "source": po.get("source"),
            "is_retweet": bool(po.get("retweeted_status") or po.get("retweet_status_id")),
            "truncated": po.get("truncated", False),
            "user_id": po.get("user_id"),
            "user_name": ((po.get("user") or {}).get("screen_name")
                          if isinstance(po.get("user"), dict) else po.get("user_name")),
            "pics": _extract_pics(po),
        })
    seen, uniq = set(), []
    for x in res:
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        uniq.append(x)
    return uniq
