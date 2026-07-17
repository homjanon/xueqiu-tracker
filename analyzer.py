"""信号识别：三级后端链（NVIDIA Qwen3.5-VL -> Kimi-K2.5 -> 硅基流动 Qwen3.5-35B）。
文字发言走 classify()，带图发言额外走 vision_extract()；无 Key 时回退启发式。
支持按用户注入专属黑话词典（USER_HINTS），并生成「每日一句话总结」。"""
import json
import re

import requests

from config import BACKENDS, TIMEOUT, STOCK_ALIASES
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
    print("[analyzer] ⚠️ 所有后端均未成功（可能 Key 缺失或全失败），将回退启发式")
    return None


def _parse_json(content):
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M)
    data = json.loads(content)
    return data if isinstance(data, list) else (data.get("signals") or [])


TEXT_PROMPT = """你是雪球持仓追踪助手。以下是某雪球大V的发言（可能含"回复@/转发//@"等，只要本人提及实盘操作都算数）。
请识别其中【明确的】买入/卖出/加仓/减仓/持有操作。规则：
1. 动作词映射：买入/建仓/加仓/上车/抄底/新入/补仓/进货/买了/加了一些=buy；卖出/减仓/清仓/出掉/出了/止损/止盈/卖掉/卖了/砍了=sell；加仓=add；减仓=reduce；持有/不动/躺平/没动/继续拿/拿着=hold。"保本出了X"=卖出X。
2. 标的不仅限 $名(代码)$ 格式：昵称/简称也算（如 酒家=广州酒家、顺丰=顺丰控股、青岛港=青岛港、小招/小昭=招商银行、大波/宁波银行=宁波银行）。请尽量写出【股票全名+代码】；即使没有代码，也照常标 action 并写出股票名。
3. 一条发言可能含多个操作、多个标的，请拆成多条记录（可同 post_id 多条）。
4. 忽略纯观点、行情吐槽、无动作词的闲聊；但只要出现上述动作词且能对应到某标的（含昵称），就提取，不要因缺少代码而丢弃。
对每条相关发言输出 JSON 数组，每条包含：
- post_id: 发言原 id（整数）
- action: buy/sell/add/reduce/hold
- stocks: 股票全名或代码列表（尽量带代码，如 ["广州酒家(SH603043)","顺丰控股(SZ002352)"]）
- confidence: high/medium/low
- evidence: 原文关键原句（截取）
只输出 JSON 数组，不要解释、不要多余文字。
"""


def _enrich_stocks(signals):
    """用 STOCK_ALIASES 把缺代码的昵称补全为标准 全名(代码)。"""
    for s in signals:
        new = []
        for st in (s.get("stocks") or []):
            repl = None
            for k, v in STOCK_ALIASES.items():
                if k in st and "(" not in st:
                    repl = v
                    break
            new.append(repl if repl else st)
        s["stocks"] = new
    return signals


def _llm_classify(posts, hint=""):
    """调用 LLM 识别操作，返回信号列表；失败/无 Key 返回 None。"""
    numbered = "\n".join(f"{i+1}. (id={p['id']}) {p['text']}" for i, p in enumerate(posts))
    user_part = ("\n\n【该用户专属黑话/习惯提示，请据此正确解读，不要因用了黑话就忽略实盘操作】\n"
                 + hint) if hint else ""
    content = call_multi([
        {"role": "system", "content": "你是雪球持仓追踪助手。只输出严格JSON数组，不要解释。"},
        {"role": "user", "content": TEXT_PROMPT + user_part + "\n发言列表：\n" + numbered},
    ])
    if not content:
        return None
    try:
        arr = _parse_json(content)
        for s in arr:
            s.setdefault("method", "llm")
        return _enrich_stocks(arr)
    except Exception as e:
        print("[analyzer] LLM 解析失败:", e)
        return None


def classify(posts, hint=""):
    """LLM 优先识别；启发式始终运行并按【动作缺口】补缺（LLM 漏了哪个方向就补哪个），合并去重。"""
    if not posts:
        return []
    llm_sigs = _llm_classify(posts, hint=hint) or []   # list（可能为空）或 None
    hep_sigs = heuristic(posts, hint=hint)            # 始终跑，作保底
    # 记录 LLM 已覆盖的 (post_id -> {action})，用于按动作补缺
    covered = {}
    for s in llm_sigs:
        covered.setdefault(s["post_id"], set()).add(s["action"])
    merged = list(llm_sigs)
    for h in hep_sigs:
        if h["action"] not in covered.get(h["post_id"], set()):  # 该帖该方向 LLM 没覆盖 → 补
            merged.append(h)
            covered.setdefault(h["post_id"], set()).add(h["action"])
    return merged


VISION_PROMPT = """这是雪球用户发的截图（可能是K线走势、持仓数量、价格等）。请识别并提取结构化信息，只输出严格JSON数组：
[{"action":"buy/sell/add/reduce/hold/none","stocks":["股票全名或代码(尽量带代码，如 招商银行(SH600036))"],"price":"识别到的价格(无则空串)","quantity":"识别到的数量/股数(无则空串)","trend":"走势描述","confidence":"high/medium/low","evidence":"关键可见文字"}]
昵称也算标的（酒家=广州酒家、顺丰=顺丰控股、小招/小昭=招商银行、大波/宁波银行）。若截图无法判断具体操作，action填none。"""


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
    """关键词兜底：同一发言若同时含买/卖词，可 emit 多条（多动作）。"""
    sig = []
    for po in posts:
        t = po["text"]
        actions = []
        if any(k in t for k in BUY_KW):
            actions.append("buy")
        if any(k in t for k in SELL_KW):      # 不再 elif：一条发言可同时含买和卖
            actions.append("sell")
        if not actions and any(k in t for k in HOLD_KW):
            actions.append("hold")
        if not actions and any(k in t for k in TRADE_MARKER_KW):
            # 出现黑话实盘标记但方向不明，标为 hold 占位并备注由 LLM 判断
            actions.append("hold")
        for action in actions:
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
