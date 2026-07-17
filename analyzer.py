"""每日讨论归纳：把多位雪球大V的发言，中性归纳成每人一句短评（≤50字）。
不再判断买卖操作——由用户自行根据归纳判断。无 Key 时回退发言摘录。"""
import json
import re

import requests

from config import BACKENDS, TIMEOUT, USER_HINTS


def _post(backend, messages):
    if not backend.get("api_key"):
        return None
    try:
        r = requests.post(
            f"{backend['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {backend['api_key']}",
                      "Content-Type": "application/json"},
            json={"model": backend["model"], "messages": messages, "temperature": 0.3},
            timeout=backend.get("timeout", TIMEOUT))
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[analyzer] {backend['name']} 调用失败: {e}")
        return None


def call_multi(messages):
    """按 BACKENDS 顺序尝试，返回首个成功的内容；全失败返回 None。"""
    for b in BACKENDS:
        c = _post(b, messages)
        if c:
            print(f"[analyzer] ✅ {b['name']} 调用成功（{b['model']}）")
            return c
    print("[analyzer] ⚠️ 所有后端均未成功（可能 Key 缺失或全失败），将回退摘录")
    return None


def _clean_think(s):
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL | re.IGNORECASE)


def _strip_fence(s):
    return re.sub(r"^```(?:json|markdown)?|```$", "", s.strip(), flags=re.M)


def _extract_text(content):
    """从模型输出提取纯文本（兼容 {"summary":...} 包裹与裸文本）。"""
    if not content:
        return None
    s = _clean_think(content)
    try:
        d = json.loads(_strip_fence(s))
        if isinstance(d, dict):
            for k in ("summary", "result", "answer", "text"):
                if isinstance(d.get(k), str) and d[k].strip():
                    return d[k].strip()
    except Exception:
        pass
    t = _strip_fence(s).strip().strip("\"'。 ").strip()
    return t or None


def _summarize_user(name, uid, posts):
    """把单个用户的发言归纳成 ≤50 字一句；无发言返回「暂未发言」。"""
    if not posts:
        return "暂未发言"
    hint = USER_HINTS.get(uid, "")
    text_block = "\n".join(f"- {p.get('text', '')}" for p in posts[:15])
    system = ("你是财经编辑。若用户用了黑话（见下方提示），请据此正确理解其讨论内容；"
              "但归纳中只做事实描述，不要判断任何买卖操作。"
              + ("\n\n黑话提示：\n" + hint if hint else ""))
    user = (f"以下是雪球用户「{name}」近期的发言原文：\n\n{text_block}\n\n"
            f"请用不超过 50 字的一两句话，中性归纳他讨论了什么（关注的市场/标的/观点/情绪等）。"
            f"只做事实性归纳，禁止出现「买入/卖出/持有/加仓/减仓」等结论性标签；不编造；"
            f"严格≤50字，无标题无列表无解释。")
    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    sent = _extract_text(out) if out else None
    if not sent:
        sent = (posts[0].get("text", "")[:50]) or "暂未发言"
    return sent[:50]   # 截断兜底，确保 ≤50 字


def daily_summary(user_infos):
    """每位用户各一句（≤50字）中性归纳；无人发言则该人显示「暂未发言」。"""
    lines = []
    for u in user_infos:
        name = u.get("name") or u.get("user_id") or "未知用户"
        uid = u.get("user_id", "")
        posts = u.get("posts") or u.get("recent_posts") or []
        lines.append(f"{name}：{_summarize_user(name, uid, posts)}")
    return "\n".join(lines)
