"""信号识别：三级后端链（NVIDIA Qwen3.5-VL -> Kimi-K2.5 -> 硅基流动 Qwen3.5-35B）。
文字发言走 classify()，带图发言额外走 vision_extract()；无 Key 时回退启发式。"""
import json
import re

import requests

from config import BACKENDS
from scraper import download_image_b64

BUY_KW = ["买入", "建仓", "加仓", "上车", "抄底", "新入", "补仓", "吸筹",
           "加了一些", "买入了", "建了仓", "搞点", "加一点"]
SELL_KW = ["卖出", "减仓", "清仓", "出掉", "出了", "止损", "止盈", "卖掉",
            "减了", "割肉", "离场", "清了"]
HOLD_KW = ["持有", "不动", "躺平", "仓位还在", "继续拿", "拿着", "没动",
            "死拿", "继续持有", "还在"]

STOCK_RE = re.compile(r"\$([^$()]+?)\(([^)]+)\)\$")


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


def classify(posts):
    if not posts:
        return []
    numbered = "\n".join(f"{i+1}. (id={p['id']}) {p['text']}" for i, p in enumerate(posts))
    content = call_multi([
        {"role": "system", "content": "你是雪球持仓追踪助手。只输出严格JSON数组，不要解释。"},
        {"role": "user", "content": TEXT_PROMPT + "\n发言列表：\n" + numbered},
    ])
    if content:
        try:
            arr = _parse_json(content)
            for s in arr:
                s.setdefault("method", "llm")
            return arr
        except Exception as e:
            print("[analyzer] LLM 解析失败，回退启发式:", e)
    return heuristic(posts)


VISION_PROMPT = """这是雪球用户发的截图（可能是K线走势、持仓数量、价格等）。请识别并提取结构化信息，只输出严格JSON数组：
[{"action":"buy/sell/add/reduce/hold/none","stocks":["股票名或代码"],"price":"识别到的价格(无则空串)","quantity":"识别到的数量/股数(无则空串)","trend":"走势描述","confidence":"high/medium/low","evidence":"关键可见文字"}]
若截图无法判断具体操作，action填none。"""


def vision_extract(post):
    pics = post.get("pics") or []
    if not pics:
        return None
    content = [{"type": "text", "text": VISION_PROMPT}]
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


def heuristic(posts):
    sig = []
    for po in posts:
        t = po["text"]
        if any(k in t for k in BUY_KW):
            action = "buy"
        elif any(k in t for k in SELL_KW):
            action = "sell"
        elif any(k in t for k in HOLD_KW):
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
