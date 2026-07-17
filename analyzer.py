"""信号识别：三级后端链（NVIDIA Qwen3.5-VL -> Kimi-K2.5 -> 硅基流动 Qwen3.5-35B）。
文字发言走 classify()，带图发言额外走 vision_extract()；无 Key 时回退启发式。
支持按用户注入专属黑话词典（USER_HINTS），并生成「每日一句话总结」。"""
import json
import re

import requests

from config import BACKENDS
from scraper import download_image_b64

BUY_KW = ["买入", "建仓", "加仓", "上车", "抄底", "新入", "补仓", "吸筹",
           "加了一些", "买入了", "建了仓", "搞点", "加一点", "进货", "买了"]
SELL_KW = ["卖出", "减仓", "清仓", "出掉", "出了", "止损", "止盈", "卖掉",
            "减了", "割肉", "离场", "清了", "卖了", "砍了"]
HOLD_KW = ["持有", "不动", "躺平", "仓位还在", "继续拿", "拿着", "没动",
            "死拿", "继续持有", "还在", "没操作", "格局", "捂着"]

# 黑话触发词：出现即代表有实盘动作，方向交由 LLM 结合上下文判断
TRADE_MARKER_KW = ["mnp", "MNP", "羊毛", "实盘", "做T", "做t"]

STOCK_RE = re.compile(r"\$([^$()]+?)\(([^)]+)\)\$")

_ACTION_CN = {"buy": "买入", "sell": "卖出", "add": "加仓",
              "reduce": "减仓", "hold": "持有", "none": "无操作"}


def _post(backend, messages):
    if not backend.get("api_key"):
        return None
    try:
        r = requests.post(
            f"{backend['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {backend['api_key']}",
                      "Content-Type": "application/json"},
            json={"model": backend["model"], "messages": messages,
                  "response_format": {"type": "json_object"}},
            timeout=90)
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
            return c
    return None


def _parse_json(content):
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M)
    data = json.loads(content)
    return data if isinstance(data, list) else (data.get("signals") or [])


TEXT_PROMPT = """以下是雪球用户今日新增发言，请识别其中【明确】的买入/卖出/加仓/减仓/持有操作，忽略模糊表态、纯观点、行情吐槽、无标的的发言。
对每条相关发言提取：
- post_id: 发言原 id
- action: buy/sell/add/reduce/hold
- stocks: 涉及的股票名或代码列表
- confidence: high/medium/low
- evidence: 原文原句（截取关键部分）
只输出 JSON 数组，例如：
[{"post_id":123,"action":"buy","stocks":["青岛港"],"confidence":"high","evidence":"加了一些青岛港h股"}]
"""


def classify(posts, hint=""):
    if not posts:
        return []
    numbered = "\n".join(f"{i+1}. (id={p['id']}) {p['text']}" for i, p in enumerate(posts))
    user_part = ("\n\n【该用户专属黑话/习惯提示，请据此正确解读，不要因用了黑话就忽略实盘操作】\n"
                 + hint) if hint else ""
    content = call_multi([
        {"role": "system", "content": "你是雪球持仓追踪助手。只输出严格JSON数组，不要解释。"},
        {"role": "user", "content": TEXT_PROMPT + user_part + "\n发言列表：\n" + numbered},
    ])
    if content:
        try:
            arr = _parse_json(content)
            for s in arr:
                s.setdefault("method", "llm")
            return arr
        except Exception as e:
            print("[analyzer] LLM 解析失败，回退启发式:", e)
    return heuristic(posts, hint=hint)


VISION_PROMPT = """这是雪球用户发的截图（可能是K线走势、持仓数量、价格等）。请识别并提取结构化信息，只输出严格JSON数组：
[{"action":"buy/sell/add/reduce/hold/none","stocks":["股票名或代码"],"price":"识别到的价格(无则空串)","quantity":"识别到的数量/股数(无则空串)","trend":"走势描述","confidence":"high/medium/low","evidence":"关键可见文字"}]
若截图无法判断具体操作，action填none。"""


def vision_extract(post, hint=""):
    pics = post.get("pics") or []
    if not pics:
        return None
    content = [{"type": "text", "text": VISION_PROMPT}]
    if hint:
        content[0]["text"] += ("\n\n【该用户专属黑话/习惯提示】\n" + hint)
    for url in pics[:4]:
        b64 = download_image_b64(url)
        if b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    if len(content) == 1:
        return None
    out = call_multi([{"role": "user", "content": content}])
    if not out:
        return None
    try:
        arr = _parse_json(out)
        for s in arr:
            s.setdefault("method", "vision")
            s["post_id"] = post["id"]
        return arr
    except Exception:
        return None


def heuristic(posts, hint=""):
    sig = []
    for po in posts:
        t = po["text"]
        if any(k in t for k in BUY_KW):
            action = "buy"
        elif any(k in t for k in SELL_KW):
            action = "sell"
        elif any(k in t for k in HOLD_KW):
            action = "hold"
        elif any(k in t for k in TRADE_MARKER_KW):
            # 出现黑话实盘标记但方向不明，标为 hold 占位并备注由 LLM 判断
            action = "hold"
        else:
            continue
        sig.append({
            "post_id": po["id"],
            "action": action,
            "stocks": [f"{m.group(1)}({m.group(2)})" for m in STOCK_RE.finditer(t)],
            "confidence": "low",
            "evidence": t[:200],
            "method": "heuristic",
        })
    return sig


def _flatten_signals(user_infos):
    """把多个用户的信号摊平成 (name, signal) 列表，供总结使用。"""
    flat = []
    for u in user_infos:
        for sig in (u.get("text_signals") or []) + (u.get("vision_signals") or []):
            flat.append((u.get("name", ""), sig))
    return flat


def daily_summary(user_infos, hint_by_name=None):
    """生成「每日一句话总结」。优先用 LLM，失败回退启发式拼接。"""
    flat = _flatten_signals(user_infos)
    if not flat:
        # 无信号时仍让 LLM 结合发言主题总结一句（兜底用启发式）
        topics = []
        for u in user_infos:
            posts = u.get("posts") or []
            if posts:
                topics.append(f"{u.get('name','')}: {posts[0]['text'][:80]}")
        if topics:
            return "今日各路大V暂无明确买卖操作，以观点与行情交流为主。" + "；".join(topics[:3])
        return "今日各路大V暂无明确买卖操作，以观点与行情交流为主。"

    lines = []
    for u in user_infos:
        name = u.get("name", "")
        sigs = (u.get("text_signals") or []) + (u.get("vision_signals") or [])
        if not sigs:
            lines.append(f"{name}:（无明确操作）")
            continue
        for s in sigs:
            stocks = "、".join(s.get("stocks") or []) or "未标注标的"
            lines.append(f"{name}: {_ACTION_CN.get(s.get('action'), s.get('action'))} {stocks}（{s.get('confidence')}）")
    prompt = ("以下是今日多位雪球大V的明确操作信号，请写【一句话】中文总结全天重点操作"
              "（谁买了/卖了/加了/减了/看好/不看好什么）；若有人无操作也带一句。"
              "只输出那一句话，不要列表、不要解释、不要输出多余标点以外的字符。\n\n"
              + "\n".join(lines))

    out = call_multi([
        {"role": "system", "content": "你是财经编辑，擅长把多条操作信号浓缩成一句精炼中文。"},
        {"role": "user", "content": prompt},
    ])
    if out:
        out = out.strip().strip("\"'。").strip()
        if out:
            return out
    # 启发式兜底
    parts = []
    for name, s in flat:
        stocks = "、".join(s.get("stocks") or []) or "未标注标的"
        parts.append(f"{name}{_ACTION_CN.get(s.get('action'), s.get('action'))}{stocks}")
    return "今日重点关注：" + "；".join(parts)
