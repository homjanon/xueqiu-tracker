"""主流程：抓 -> 清洗 -> 去重 -> 文字识别 + 图片识别 -> 写仓库数据/报告 -> 更新状态。"""
import datetime
import json
import os

from config import (XUEQIU_USER_ID, PAGES, HEADLESS, DATA_DIR, REPORT_DIR, STATE_FILE)
from scraper import fetch_timeline, normalize
from analyzer import classify, vision_extract

CST = datetime.timezone(datetime.timedelta(hours=8))


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"last_post_id": 0, "updated_at": ""}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def build_report(ts, new, text_sig, vision_sig):
    L = [f"# 雪球大V动态追踪 · {ts}", "",
          f"- 新增发言：**{len(new)}** 条｜文字信号：**{len(text_sig)}**｜图片信号：**{len(vision_sig)}**", ""]
    if text_sig:
        L.append("## 文字操作信号")
        for s in text_sig:
            stocks = "、".join(s.get("stocks") or []) or "（未标注代码）"
            L.append(f"- **{s['action']}** {stocks} ｜ 置信度:{s.get('confidence')} ｜ 来源:{s.get('method')}")
            L.append(f"  > {s.get('evidence','')[:200]}")
        L.append("")
    if vision_sig:
        L.append("## 图片操作信号")
        for s in vision_sig:
            stocks = "、".join(s.get("stocks") or []) or "（未识别）"
            extra = []
            if s.get("price"): extra.append(f"价格:{s['price']}")
            if s.get("quantity"): extra.append(f"数量:{s['quantity']}")
            if s.get("trend"): extra.append(f"走势:{s['trend']}")
            L.append(f"- **{s.get('action')}** {stocks} {' '.join(extra)} ｜ 置信度:{s.get('confidence')} ｜ 来源:{s.get('method')}")
            L.append(f"  > {s.get('evidence','')[:200]}")
        L.append("")
    L.append("## 原始新增发言")
    for p in new[:30]:
        pic = " [图]" if p.get("pics") else ""
        L.append(f"- ({p['id']}){pic} {p['text'][:200]}")
    return "\n".join(L)


def main():
    st = load_state()
    last_id = st.get("last_post_id", 0)
    print(f"[状态] 上次最大帖子ID: {last_id}")

    raw = fetch_timeline(XUEQIU_USER_ID, pages=PAGES, headless=HEADLESS)
    posts = normalize(raw)
    print(f"[抓取] 去重后 {len(posts)} 条")

    new = [p for p in posts if (p["id"] or 0) > last_id]
    print(f"[增量] 新增 {len(new)} 条")

    text_sig = classify(new) if new else []
    vision_sig = []
    for p in new:
        v = vision_extract(p)
        if v:
            vision_sig.extend(v)
    if vision_sig:
        print(f"[图片] 识别到 {len(vision_sig)} 条图片信号")

    now = datetime.datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    latest = {
        "user_id": XUEQIU_USER_ID,
        "fetched_at": ts,
        "new_count": len(new),
        "text_signal_count": len(text_sig),
        "vision_signal_count": len(vision_sig),
        "posts": new,
        "text_signals": text_sig,
        "vision_signals": vision_sig,
    }
    with open(f"{DATA_DIR}/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    md = build_report(ts, new, text_sig, vision_sig)
    with open(f"{REPORT_DIR}/{now.strftime('%Y-%m-%d')}.md", "w", encoding="utf-8") as f:
        f.write(md)

    if new:
        st["last_post_id"] = max([p["id"] for p in posts] + [last_id])
    st["updated_at"] = ts
    save_state(st)

    print(f"[完成] data/latest.json 已更新（网站读取此文件）；报告 reports/{now.strftime('%Y-%m-%d')}.md")


if __name__ == "__main__":
    main()
