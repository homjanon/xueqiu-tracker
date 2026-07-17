"""主流程（多用户）：抓 -> 清洗 -> 去重 -> 文字识别 + 图片识别 -> 生成每日一句话总结
-> 写 data/latest.json（顶层合并 + users[] 明细 + daily_summary）与 reports/YYYY-MM-DD.md -> 更新状态。

latest.json 采用「顶层合并 + users[] 明细」双结构：
  - 顶层 posts/text_signals/vision_signals 为所有用户合并（老网站零改动可直接读）
  - users[] 为每用户明细；daily_summary 为 AI 一句话总结（网站新增可调取字段）
"""
import datetime
import json
import os

from config import (XUEQIU_USER_IDS, USER_HINTS, PAGES, HEADLESS,
                    DATA_DIR, REPORT_DIR, STATE_FILE)
from scraper import fetch_timeline, normalize
from analyzer import classify, vision_extract, daily_summary

CST = datetime.timezone(datetime.timedelta(hours=8))


def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("updated_at", "")
    st.setdefault("users", {})
    # 从老的单用户格式迁移
    if "last_post_id" in st and not st["users"]:
        for uid in XUEQIU_USER_IDS:
            st["users"][uid] = {"last_post_id": st["last_post_id"], "name": ""}
    for uid in XUEQIU_USER_IDS:
        st["users"].setdefault(uid, {"last_post_id": 0, "name": ""})
    return st


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def process_user(uid, state_users):
    hint = USER_HINTS.get(uid, "")
    last_id = state_users.get(uid, {}).get("last_post_id", 0)
    print(f"\n=== 用户 {uid}（上次最大ID: {last_id}）===")

    raw = fetch_timeline(uid, pages=PAGES, headless=HEADLESS)
    posts = normalize(raw)
    print(f"[抓取] 去重后 {len(posts)} 条")

    name = posts[0]["user_name"] if posts and posts[0].get("user_name") else state_users.get(uid, {}).get("name", "")
    new = [p for p in posts if (p["id"] or 0) > last_id]
    print(f"[增量] 新增 {len(new)} 条")

    text_sig = classify(new, hint=hint) if new else []
    vision_sig = []
    for p in new:
        v = vision_extract(p, hint=hint)
        if v:
            vision_sig.extend(v)
    if vision_sig:
        print(f"[图片] 识别到 {len(vision_sig)} 条图片信号")

    if new:
        state_users[uid] = {"last_post_id": max([p["id"] for p in posts] + [last_id]), "name": name}

    return {
        "user_id": uid,
        "name": name,
        "new_count": len(new),
        "text_signal_count": len(text_sig),
        "vision_signal_count": len(vision_sig),
        "posts": new,
        "text_signals": text_sig,
        "vision_signals": vision_sig,
    }


def build_report(ts, summary, users):
    L = [f"# 雪球大V动态追踪 · {ts}", "",
         f"- 跟踪用户：**{len(users)}** ｜ 新增发言：**{sum(u['new_count'] for u in users)}** 条",
         f"- 文字信号：**{sum(u['text_signal_count'] for u in users)}** ｜ 图片信号：**{sum(u['vision_signal_count'] for u in users)}**", ""]
    L.append("## AI 一句话总结")
    L.append(f"> {summary}")
    L.append("")
    for u in users:
        name = u["name"] or u["user_id"]
        L.append(f"## {name}（{u['user_id']}）· 新增 {u['new_count']} 条")
        sigs = u["text_signals"] + u["vision_signals"]
        if sigs:
            L.append("### 操作信号")
            for s in sigs:
                stocks = "、".join(s.get("stocks") or []) or "（未标注代码）"
                extra = []
                if s.get("price"): extra.append(f"价格:{s['price']}")
                if s.get("quantity"): extra.append(f"数量:{s['quantity']}")
                L.append(f"- **{s.get('action')}** {stocks} {' '.join(extra)} ｜ 置信度:{s.get('confidence')} ｜ 来源:{s.get('method')}")
                L.append(f"  > {s.get('evidence','')[:200]}")
        else:
            L.append("- 无明确买卖操作信号")
        L.append("")
        L.append("### 原始新增发言")
        for p in u["posts"][:30]:
            pic = " [图]" if p.get("pics") else ""
            L.append(f"- ({p['id']}){pic} {p['text'][:200]}")
        L.append("")
    return "\n".join(L)


def main():
    st = load_state()
    state_users = st["users"]
    print(f"[状态] 跟踪用户: {', '.join(XUEQIU_USER_IDS)}")

    users = []
    for uid in XUEQIU_USER_IDS:
        try:
            users.append(process_user(uid, state_users))
        except Exception as e:
            print(f"[错误] 用户 {uid} 处理失败，跳过: {e}")

    now = datetime.datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    # AI 每日一句话总结（跨用户合并）
    summary = daily_summary(users) if users else "今日无新增发言。"
    print(f"[总结] {summary}")

    merged_posts = [p for u in users for p in u["posts"]]
    merged_text = [dict(s, **{"_user": u["name"] or u["user_id"]})
                   for u in users for s in u["text_signals"]]
    merged_vision = [dict(s, **{"_user": u["name"] or u["user_id"]})
                     for u in users for s in u["vision_signals"]]

    latest = {
        "fetched_at": ts,
        "daily_summary": summary,
        "user_count": len(users),
        "new_count": sum(u["new_count"] for u in users),
        "text_signal_count": sum(u["text_signal_count"] for u in users),
        "vision_signal_count": sum(u["vision_signal_count"] for u in users),
        # 顶层合并（老网站零改动可读）
        "posts": merged_posts,
        "text_signals": merged_text,
        "vision_signals": merged_vision,
        # 每用户明细
        "users": users,
    }
    with open(f"{DATA_DIR}/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    md = build_report(ts, summary, users)
    with open(f"{REPORT_DIR}/{now.strftime('%Y-%m-%d')}.md", "w", encoding="utf-8") as f:
        f.write(md)

    st["updated_at"] = ts
    save_state(st)

    print(f"[完成] data/latest.json 已更新（网站可读顶层合并字段，亦可单独读 daily_summary）；"
          f"报告 reports/{now.strftime('%Y-%m-%d')}.md")


if __name__ == "__main__":
    main()
