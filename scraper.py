"""雪球抓取：拿 xq_a_token -> 调 JSON 接口；并提取/下载帖子图片。

2026-10-23 修复（10-21~10-23 连续空报告）：
  根因 = 裸域 `xueqiu.com` 已挂腾讯云 WAF（响应头 acw_tc + body 为
  `{"_waf_bd8ce2ce37":...}` JS 挑战页），在数据中心 IP（GitHub Actions）上
  挑战不通过 -> 拿不到 xq_a_token -> 旧代码 except 吞异常 return [] -> 空报告。
  实测 `www.xueqiu.com` 无该挑战（HTTP 直连即下发 token），故改为：
    通道1（首选）：纯 requests 打 www 首页拿 token（无浏览器，快且稳）
    通道2（兜底）：Playwright 打 www（仍避开裸域）+ 轮询等 token
  两通道都失败 -> 抛 ScrapeError，由 tracker 决定报红，不再静默返回 []。
  另：匿名调用 `page>=2` 恒返回 400「请登录雪球查看更多内容」，故只取第 1 页；
  请求间加 gap 避免 400「访问频率太快了」。
"""
import base64
import re
import time
from io import BytesIO

import requests
from PIL import Image
from playwright.sync_api import sync_playwright

# ⚠️ 必须是 www 子域：裸域 xueqiu.com 会返回 WAF JS 挑战页，纯 HTTP 拿不到 token
BASE = "https://www.xueqiu.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

IMG_HEADERS = {"User-Agent": UA, "Referer": "https://xueqiu.com/"}

# 匿名态只允许翻到第 1 页（page>=2 恒 400「请登录雪球查看更多内容」）
MAX_ANON_PAGES = 1
# 同一会话连续请求的最小间隔，规避 400「访问频率太快了，请休息下」
REQ_GAP = 1.8


class ScrapeError(RuntimeError):
    """抓取失败（区别于「抓取成功但该用户没发帖」）。"""


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


# ---------------------------------------------------------------- 通道1：纯 HTTP

def _new_session():
    s = requests.Session()
    s.trust_env = False          # CI 无需代理；本地可避开系统代理干扰
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def _session_with_token():
    """打 www 首页拿匿名 xq_a_token；失败抛 ScrapeError。"""
    s = _new_session()
    r = s.get(BASE + "/", timeout=25)
    if r.status_code != 200:
        raise ScrapeError(f"www 首页 HTTP {r.status_code}")
    if not s.cookies.get("xq_a_token"):
        # 命中挑战页时 body 里会带 _waf_ 标记，便于日志定位
        waf = "_waf_" in r.text[:400]
        raise ScrapeError(f"www 首页未下发 xq_a_token（WAF挑战={waf}）")
    return s


def _timeline_http(sess, user_id, pages):
    """返回 (posts, first_status)。first_status 仅取第 1 页，用于区分「失败」与「真没帖」。"""
    out, first_status = [], None
    pages = max(1, min(pages, MAX_ANON_PAGES))
    for pg in range(1, pages + 1):
        if pg > 1:
            time.sleep(REQ_GAP)
        r = sess.get(f"{BASE}/statuses/user_timeline.json",
                     params={"user_id": user_id, "page": pg, "num": 20,
                             "type": "status", "method": "forUM"},
                     headers={"Referer": f"{BASE}/u/{user_id}",
                              "X-Requested-With": "XMLHttpRequest",
                              "Accept": "application/json"},
                     timeout=30)
        if pg == 1:
            first_status = r.status_code
        if r.status_code != 200:
            print(f"[scraper] 第 {pg} 页 HTTP {r.status_code}，停止翻页")
            break
        try:
            out.extend((r.json() or {}).get("statuses") or [])
        except Exception as e:
            print(f"[scraper] 第 {pg} 页 JSON 解析失败: {e}")
            break
    return out, first_status


# ---------------------------------------------------------------- 通道2：Playwright

def _timeline_browser(user_id, pages, headless, timeout):
    """Playwright 兜底：打 www（避开裸域挑战），轮询等待 xq_a_token 最多 20s。"""
    out = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, timeout=30000)
        try:
            ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                      viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            _apply_stealth(page)
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30000)

            # 轮询等 token（原来死等 6s，机器慢/挑战慢就误判失败）
            deadline = time.time() + 20
            while time.time() < deadline:
                if any(c["name"] == "xq_a_token" for c in ctx.cookies()):
                    break
                time.sleep(1)
            else:
                raise ScrapeError("浏览器通道未拿到 xq_a_token")

            pages = max(1, min(pages, MAX_ANON_PAGES))
            for pg in range(1, pages + 1):
                if pg > 1:
                    time.sleep(REQ_GAP)
                api = (f"{BASE}/statuses/user_timeline.json?"
                       f"user_id={user_id}&page={pg}&num=20&type=status&method=forUM")
                r = ctx.request.get(api, headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{BASE}/u/{user_id}",
                    "Accept": "application/json"}, timeout=30000)
                if r.status != 200:
                    print(f"[scraper] 浏览器通道第 {pg} 页 HTTP {r.status}，停止翻页")
                    break
                out.extend((r.json() or {}).get("statuses") or [])
        finally:
            browser.close()
    return out


# ---------------------------------------------------------------- 对外入口

def fetch_timeline(user_id, pages=2, headless=True, timeout=180):
    """抓取用户时间线（双通道）。失败抛 ScrapeError —— 绝不静默返回空列表。

    返回 [] 只代表「抓取成功、该用户没有可见帖子」，由调用方正常处理。
    """
    t0 = time.time()

    # 通道1：纯 HTTP（首选）
    try:
        sess = _session_with_token()
        posts, status = _timeline_http(sess, user_id, pages)
        if status == 200:
            print(f"[scraper] ✓ HTTP 通道 {len(posts)} 条 / {time.time()-t0:.1f}s")
            return posts
        print(f"[scraper] ⚠️ HTTP 通道第1页 HTTP {status}，转浏览器兜底")
    except Exception as e:
        print(f"[scraper] ⚠️ HTTP 通道失败: {e}")

    # 通道2：Playwright 兜底
    try:
        posts = _timeline_browser(user_id, pages, headless, timeout)
        if posts:
            print(f"[scraper] ✓ 浏览器通道 {len(posts)} 条 / {time.time()-t0:.1f}s")
            return posts
        raise ScrapeError("浏览器通道返回空")
    except Exception as e:
        print(f"[scraper] ⛔ 浏览器通道失败: {e}")

    raise ScrapeError(f"双通道均失败（耗时 {time.time()-t0:.1f}s，用户 {user_id}）")


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
