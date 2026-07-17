"""信号识别：三级后端链（NVIDIA Qwen3.5-VL -> Kimi-K2.5 -> 硅基流动 Qwen3.5-35B）。
文字发言走 classify()，带图发言额外走 vision_extract()；无 Key 时回退启发式。
支持按用户注入专属黑话词典（USER_HINTS），并生成「每日一句话总结」。"""
import json
import os
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
                  "temperature": 0.2},
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


def _clean_think(s):
    """去除模型的 <think>...</think> 推理块（部分推理模型会在 json_object 外额外输出）。"""
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL | re.IGNORECASE)


def _strip_fence(s):
    return re.sub(r"^```(?:json|markdown)?|```$", "", s.strip(), flags=re.M)


def _extract_json(content):
    """鲁棒提取 JSON 数组：兼容 裸数组 / {"signals":[...]} / 推理对象包裹 / 带围栏。
    返回 list；无法提取或对象中无 signals 时返回 []（交由启发式兜底）。"""
    if not content:
        return []
    s = _clean_think(content)
    s = _strip_fence(s)
    # 1) 尝试整体解析
    data = None
    try:
        data = json.loads(s)
    except Exception:
        data = None
    # 2) 括号配平截取第一个 { 或 [
    if data is None:
        m = re.search(r"[\[{]", s)
        if not m:
            return []
        start = m.start()
        opener, closer = s[start], ("}" if s[start] == "{" else "]")
        depth = 0
        end = None
        for i in range(start, len(s)):
            if s[i] == opener:
                depth += 1
            elif s[i] == closer:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return []
        try:
            data = json.loads(s[start:end])
        except Exception:
            return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("signals"), list):
            return data["signals"]
        # 退回：取第一个值为数组的字段（兼容偶发的 {"result":[...]}）
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return []  # 有对象但无 signals（如推理对象）→ 视为空
    return []


def _extract_text(content):
    """从模型输出提取纯文本（用于 daily_summary）：兼容 {"summary":"..."} 包裹与裸文本。"""
    if not content:
        return None
    s = _clean_think(content)
    # 若模型仍包成 {"summary":...} / {"result":...} 等对象，取其中的字符串
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


# 内联兜底提示词（当 prompt/extract_prompt.txt 缺失时使用）
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

PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt", "extract_prompt.txt")


def _load_extract_prompt():
    """加载结构化提取提示词（portfolio 风格）；缺失则用内联兜底。"""
    if os.path.exists(PROMPT_FILE):
        return open(PROMPT_FILE, encoding="utf-8").read()
    return TEXT_PROMPT


def _classify_nature(post):
    """判定发言性质，提示模型区分本人操作与引用他人（降噪）。"""
    t = post.get("text", "")
    if post.get("is_retweet"):
        return "转发（转发他人发言，转发内容不算本人操作）"
    if "//@" in t or "回复@" in t or t.startswith("回复 "):
        return "原创主帖（含对他人回复/引用，引用链以 //@ 标明，引用部分不算本人操作）"
    return "原创"


def _prelabel_nicknames(text):
    """预标注文本中出现的昵称（仅列出昵称本身，不强行映射，尊重用户原文叫法）。"""
    found = [k for k, v in STOCK_ALIASES.items() if k in text]
    return "、".join(found) if found else "（无）"


def _build_user_doc(posts):
    """把发言组装成 portfolio 式结构化文档：每条一个带元数据的分块。"""
    blocks = []
    for i, p in enumerate(posts, 1):
        created = p.get("created_at") or "未知"
        nature = _classify_nature(p)
        nicks = _prelabel_nicknames(p.get("text", ""))
        blocks.append(
            f"### 发言 #{i}\n"
            f"- 发言ID: {p['id']}\n"
            f"- 发布时间: {created}\n"
            f"- 性质: {nature}\n"
            f"- 系统检测到昵称: {nicks}\n"
            f"- 原文: {p['text']}\n"
        )
    return "# 用户发言文档\n\n" + "\n".join(blocks)


def _heuristic_stocks(t):
    """从文本提取标的：优先 $全名(代码)$，并补充文中昵称（保留原文叫法，不强行转代码）。"""
    stocks = [f"{m.group(1)}({m.group(2)})" for m in STOCK_RE.finditer(t)]
    existing = " ".join(stocks)
    for k in STOCK_ALIASES:
        if k in t and k not in existing:   # 避免「招商银行」已存在时又补「招行」
            stocks.append(k)
    return stocks


def _nicknames_in_text(text):
    """从原文中提取出现的昵称（作为 stocks 为空时的兜底展示）。"""
    if not text:
        return []
    return [k for k in STOCK_ALIASES if k in text]


def _llm_classify(posts, hint=""):
    """调用 LLM 识别操作，返回信号列表；失败/无 Key 返回 None。"""
    system = _load_extract_prompt()
    system = system.replace("__HINT__", ("【该用户专属黑话/习惯提示】\n" + hint) if hint else "（无）")
    doc = _build_user_doc(posts)
    content = call_multi([
        {"role": "system", "content": system},
        {"role": "user", "content": doc},
    ])
    if not content:
        return None
    try:
        arr = _extract_json(content)
        for s in arr:
            s.setdefault("method", "llm")
        return arr   # 保留 LLM 输出的原文叫法（昵称），不再强制映射为全名(代码)
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
        arr = _extract_json(out)
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
                "stocks": _heuristic_stocks(t),
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
    post_text = {}
    for u in user_infos:
        for p in (u.get("posts") or []):
            post_text[p["id"]] = p.get("text", "")
    for u in user_infos:
        name = u.get("name", "")
        sigs = (u.get("text_signals") or []) + (u.get("vision_signals") or [])
        if not sigs:
            lines.append(f"{name}:（无明确操作）")
            continue
        for s in sigs:
            stocks = s.get("stocks") or []
            if not stocks:
                stocks = _nicknames_in_text(post_text.get(s.get("post_id"), ""))
            stocks_str = "、".join(stocks) if stocks else "未标注标的"
            lines.append(f"{name}: {_ACTION_CN.get(s.get('action'), s.get('action'))} {stocks_str}（{s.get('confidence')}）")
    prompt = ("以下是今日多位雪球大V的明确操作信号，请写【一句话】中文总结全天重点操作"
              "（谁买了/卖了/加了/减了/看好/不看好什么）；若有人无操作也带一句。"
              "只输出那一句话，不要列表、不要解释、不要输出多余标点以外的字符。\n\n"
              + "\n".join(lines))

    out = call_multi([
        {"role": "system", "content": "你是财经编辑，擅长把多条操作信号浓缩成一句精炼中文。只输出那一句话，不要列表、不要解释、不要推理过程。"},
        {"role": "user", "content": prompt},
    ])
    if out:
        txt = _extract_text(out)
        if txt and txt.lower() != "null":
            return txt
    # 启发式兜底
    parts = []
    for name, s in flat:
        stocks = "、".join(s.get("stocks") or []) or "未标注标的"
        parts.append(f"{name}{_ACTION_CN.get(s.get('action'), s.get('action'))}{stocks}")
    return "今日重点关注：" + "；".join(parts)
