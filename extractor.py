"""标的提及追踪：从大V发言中抽取「提到了哪个标的 + 对应哪句原话 + 有没有写数量」。

设计原则（详见 xueqiu-mentions-schema.md）：
  - 不判断买卖方向。方向由人看「原文摘录」自行判断，AI 只做填空题。
  - AI 只填 raw_name / quote / qty 三个空（外加账户级 position / pnl）。
    其余字段（时间、规范名、次数、历史）全部由代码组装，AI 碰不到。
  - AI 输出必过三道校验闸，任何编造/改写都会被原文子串比对拦下。
  - 无 API Key 或全后端失败时，自动回退到词典扫描模式（本地预览即走此路）。
  - mentions.json 为累积文件，按 post_id 增量合并，绝不从零重建
    （因 RECENT_N=10，latest.json 每天重建，重算会导致老标的凭空消失）。
"""
import json
import re

from config import (SYMBOL_ALIAS, MAX_MENTIONS_PER_POST, HISTORY_KEEP,
                    USER_HINTS)

SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 正文切分

# 「回复@某人:」开头前缀（原创正文在其后）
_RE_REPLY_PREFIX = re.compile(r"^回复@[^:：]{1,30}[:：]\s*")
# 雪球客户端在图片位置留下的占位文字
_RE_NOISE = re.compile(r"(查看图片|网页链接|查看全文|展开全文)")


def split_original(text):
    """切出「本人原创」正文：去掉回复前缀，并切断 //@ 及其之后的全部引用内容。

    这一步是准确率的地基。转发引用里既可能是别人的持仓（如网友说「我全仓招行，
    一点点建行」），也可能是本人几天前的旧发言被反复引用；不切断会造成张冠李戴
    和同一笔操作重复计入。
    """
    if not text:
        return ""
    body = text.split("//@")[0]
    body = _RE_REPLY_PREFIX.sub("", body)
    body = _RE_NOISE.sub("", body)
    return body.strip()


_RE_SPEAKER = re.compile(r"^([^:：]{1,30})[:：]\s*(.*)$", re.DOTALL)


def split_self_quoted(text, self_name):
    """取出 //@本人: 的引用段 —— 这是本人更早的发言，被自己转引回来。

    这类内容可用于补全「总仓位/账户盈亏」（覆盖式字段，重复无害），
    但**绝不用于标的提取** —— 同一笔旧操作被反复引用会天天重复计入。
    """
    if not text or not self_name:
        return ""
    out = []
    for seg in text.split("//@")[1:]:
        m = _RE_SPEAKER.match(seg)
        if not m or m.group(1).strip() != self_name:
            continue
        body = _RE_REPLY_PREFIX.sub("", m.group(2))
        out.append(_RE_NOISE.sub("", body).strip())
    return " ".join(x for x in out if x)


# ---------------------------------------------------------------- 标的归一

def _alias_table():
    """展开成 [(别名小写, 规范名)]，按别名长度降序 —— 保证最长优先匹配，
    避免「青岛港h股」被短别名「青岛港」抢先切走。"""
    pairs = []
    for canon, aliases in SYMBOL_ALIAS.items():
        pairs.append((canon.lower(), canon))
        for a in aliases:
            pairs.append((a.lower(), canon))
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


_ALIAS_TABLE = _alias_table()


def normalize_symbol(raw_name):
    """把原文叫法归一成规范名。返回 (规范名, 是否命中词典)。"""
    s = (raw_name or "").strip()
    if not s:
        return "", False
    low = s.lower()
    for alias, canon in _ALIAS_TABLE:
        if alias == low:
            return canon, True
    for alias, canon in _ALIAS_TABLE:
        if alias in low or low in alias:
            return canon, True
    return s, False


# ---------------------------------------------------------------- 句子摘取

_RE_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
_RE_CLAUSE_SPLIT = re.compile(r"(?<=[，,])")

# 一级句超过此长度时，降到逗号子句粒度再摘，避免一句话把三个标的的操作全裹进去
_SENT_MAX = 40
# 子句短于此长度时向前借一个子句补语境，避免摘出「顺丰是」这种残句
_CLAUSE_MIN = 10


def _sentences(text):
    parts = [p.strip() for p in _RE_SENT_SPLIT.split(text) if p and p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _pick_clause(sent, keyword):
    """在长句内降到逗号子句粒度，取含该标的的最小完整片段。"""
    clauses = [c.strip() for c in _RE_CLAUSE_SPLIT.split(sent) if c and c.strip()]
    for i, c in enumerate(clauses):
        if keyword in c:
            if len(c.rstrip("，,")) < _CLAUSE_MIN and i > 0:
                return (clauses[i - 1] + c).strip()
            return c.strip()
    return sent


def _pick_sentence(text, keyword):
    """摘出含该标的的那一句。一条帖提到多个标的时，各配各的句子。"""
    for sent in _sentences(text):
        if keyword in sent:
            if len(sent) > _SENT_MAX:
                sent = _pick_clause(sent, keyword)
            return sent.strip(" ，,。")
    return text.strip(" ，,。")


# ---------------------------------------------------------------- 数量识别

_RE_QTY = re.compile(r"^\d+(?:\.\d+)?(?:手|股|万股|万元|万|元|w|W)?$")
_RE_QTY_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*(手|股|万股|万元|万|元)")


def _valid_qty(q):
    q = (q or "").strip()
    return q if q and _RE_QTY.match(q) else ""


def _find_qty(sentence, keyword):
    """在句中找靠近标的的数量。距离超过 15 字视为无关，不采信。"""
    best, best_dist = "", 999
    kpos = sentence.find(keyword)
    if kpos < 0:
        return ""
    for m in _RE_QTY_IN_TEXT.finditer(sentence):
        dist = abs(m.start() - kpos)
        if dist < best_dist:
            best, best_dist = m.group(0).replace(" ", ""), dist
    return best if best_dist <= 15 else ""


# ---------------------------------------------------------------- 账户级信息
#
# 账户栏「仓位 / 盈利」两段，规则固化如下（均为覆盖式字段，重复提取无害）：
#
# 【仓位 position】—— 抽取账户整体仓位描述，候选模式按优先级：
#   状态词：满融 / 满仓 / 空仓 / 清仓 / 半仓
#   百分比：买到X%仓位 / X%的仓位 / X%仓位 / 仓位X% / 仓位X成 / X成仓位 / X成仓
#   仅取该帖第一个命中（整段原字保留）。
#
# 【盈利 pnl】—— 抽取账户整体盈亏/收益率描述，必须带「账户级标记」以防把
#   个股盈亏算到账户头上。账户级标记分三类（见 _RE_PNL 三分支）：
#   ① 带日期：截止/截至 …（亏|盈|赚|收益|涨|跌|浮亏|浮盈|回正|回本）…（含数字）
#   ② 带年度区间：今年/年内/比年初/年初至今/本月 …（亏|盈|赚|收益|涨|跌|浮亏|
#                 浮盈|回正|回本|跑过|跑赢|跑输）…
#   ③ 带账户词：账户/整体/大盘/上证/总/总体 …（亏|盈|赚|收益|正|负|回正|回本|
#                 跑过|跑赢|跑输）…
#   另有 收益率 …（跑过|跑赢|跑输|为正|转负|回正）… 兜底分支。
#   同帖多个命中时按 _PNL_MARKERS 打分取最「账户级」者（账户/大盘/收益/年内等
#   优先于孤立的「今年X股赚Y%」），避免个股盈亏污染账户盈利。

_RE_POSITION = re.compile(
    r"(满融|满仓|空仓|清仓|半仓|半满|[\d.]+\s*成仓位|[\d.]+\s*成仓|仓位[\d.]+\s*成|"
    r"仓位[\d.]+\s*%|[\d.]+\s*%\s*的?仓位|买到[\d.]+\s*%的?仓位|[\d.]+\s*%仓位)")

_RE_PNL = re.compile(
    r"("
    # ① 带日期
    r"(?:截止|截至)[^。；\n]{0,30}?(?:亏|盈|赚|收益|涨|跌|浮亏|浮盈|回正|回本)[^。；\n]{0,16}"
    # ② 带年度区间
    r"|[^，。；\n]{0,6}(?:今年|年内|比年初|年初至今|本月)[^，。；\n]{0,18}"
        r"(?:亏|盈|赚|收益|涨|跌|浮亏|浮盈|回正|回本|跑过|跑赢|跑输)[^，。；\n]{0,12}"
    # ③ 带账户词
    r"|(?:账户|整体|大盘|上证|总|总体)[^，。；\n]{0,12}"
        r"(?:亏|盈|赚|收益|正|负|回正|回本|跑过|跑赢|跑输)[^，。；\n]{0,12}"
    # ④ 收益率兜底
    r"|收益[^，。；\n]{0,18}(?:跑过|跑赢|跑输|为正|转负|回正|已经回正|正|负)"
    r")")

# 账户级标记：命中越多越像「账户整体盈亏」而非个股盈亏
_PNL_MARKERS = ["账户", "总", "整体", "大盘", "上证", "收益", "市场",
                "年内", "比年初", "今年", "年初至今", "本月", "截止", "截至"]


def _best_pnl(text):
    """同帖多个盈利命中时，按账户级程度打分取最优。"""
    best, best_score = "", -1
    for m in _RE_PNL.finditer(text):
        s = m.group(1).strip(" ，，。")
        if not s:
            continue
        score = 1
        for mk in _PNL_MARKERS:
            if mk in s:
                score += 2
        if score > best_score:
            best_score, best = score, s
    return best


def _scan_account(text):
    pos = _RE_POSITION.search(text)
    return {
        "position": pos.group(1).strip() if pos else "",
        "pnl": _best_pnl(text),
    }


# ---------------------------------------------------------------- 词典兜底提取

def fallback_extract(text):
    """无 AI 时的词典扫描模式：按别名表在正文中找标的，最长优先且不重叠。

    对词典内已知标的，效果与 AI 基本一致；AI 的增量价值在于发现词典外的新标的。
    """
    hits = []
    low = text.lower()
    for alias, canon in _ALIAS_TABLE:
        start = 0
        while True:
            i = low.find(alias, start)
            if i < 0:
                break
            hits.append((i, i + len(alias), canon, text[i:i + len(alias)]))
            start = i + 1

    # 贪心去重叠：位置升序、长度降序，被更长匹配覆盖的丢弃
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    chosen, occupied = [], []
    for h in hits:
        if any(not (h[1] <= s or h[0] >= e) for s, e in occupied):
            continue
        occupied.append((h[0], h[1]))
        chosen.append(h)

    mentions, seen = [], set()
    for _, _, canon, raw in chosen:
        if canon in seen:          # 同帖同标的只取第一次
            continue
        seen.add(canon)
        sent = _pick_sentence(text, raw)
        mentions.append({"raw_name": raw, "quote": sent,
                         "qty": _find_qty(sent, raw)})
    return {"mentions": mentions, "account": _scan_account(text)}


# ---------------------------------------------------------------- AI 填空

PROMPT_SYSTEM = """你是信息抽取器，只做摘录，不做判断，不做推理。

规则：
1 只抽取原文中点名出现的具体股票/ETF名称。「登股」「港口股」「消费」「银行股」这类泛称一律不抽。
2 raw_name 必须与原文用字完全一致，不要改成规范名或股票代码。
3 quote 必须是从原文中原样复制的一句话，包含 raw_name 本身。不要改写、不要合并多句、不要补全省略。一条帖提到多个标的时，每个标的各配自己的那一句。
4 qty 只填原文明写的数量或金额（如 109手、5万元）。仓位百分比不填在这里。
5 account.position 填总仓位描述（如 满融、X%仓位），account.pnl 填账户整体盈亏/收益率描述（含「截止X日亏0.03%」「今年收益率跑过上证」这类定性表述），原文没写就留空。只填账户整体，不要把某只个股的盈亏算进来。
6 全文没有任何具体标的时，mentions 返回空数组。
7 只输出 JSON，不要解释，不要 markdown 代码块。

示例1
输入：再加五个点仓位，新入顺丰广州酒家马应龙青岛港这些登股。
输出：{"mentions":[{"raw_name":"顺丰","quote":"新入顺丰广州酒家马应龙青岛港这些登股","qty":""},{"raw_name":"广州酒家","quote":"新入顺丰广州酒家马应龙青岛港这些登股","qty":""},{"raw_name":"马应龙","quote":"新入顺丰广州酒家马应龙青岛港这些登股","qty":""},{"raw_name":"青岛港","quote":"新入顺丰广州酒家马应龙青岛港这些登股","qty":""}],"account":{"position":"","pnl":""}}

示例2
输入：雪球上股神特别多，动不动就是3年3倍。
输出：{"mentions":[],"account":{"position":"","pnl":""}}"""


def _ai_extract(text, uid):
    """调 AI 填空。失败返回 None，由调用方回退词典模式。"""
    try:
        from analyzer import call_multi
    except Exception:
        return None
    hint = USER_HINTS.get(str(uid), "")
    system = PROMPT_SYSTEM + (("\n\n该用户黑话提示：\n" + hint) if hint else "")
    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": f"输入：{text}\n输出："}])
    if not out:
        return None
    s = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL | re.I)
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except Exception:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("mentions"), list):
        return None
    return d


def _ai_extract_batch(posts, uid):
    """把某用户多条新帖合并为【一次】LLM 调用，返回 {idx: payload}；失败返回 None。

    2026-08-20 优化：mentions 由逐帖调用（20 条新帖=20 次 LLM）改为每用户一次批量，
    大幅减少调用次数与 NVIDIA 429 限流。输出格式：
      {"posts": [{"idx": 0, "mentions": [...], "account": {...}}, ...]}
    idx 对应输入 posts 的下标；没有提及的帖子也返回空 mentions。
    posts 元素兼容两种形态：str（split_original 后的正文，update_mentions 实际传入）
    或 dict（{text,...} 原始帖子对象，防御兼容）。
    """
    if not posts:
        return None
    try:
        from analyzer import call_multi
    except Exception:
        return None
    hint = USER_HINTS.get(str(uid), "")
    system = PROMPT_SYSTEM + (("\n\n该用户黑话提示：\n" + hint) if hint else "")
    lines = "\n".join("[%d] %s" % (i, (p if isinstance(p, str)
                                       else (p.get("text", "") or ""))[:300])
                      for i, p in enumerate(posts))
    user = ("以下是该用户最近的多条发言（[i] 为编号）：\n\n" + lines +
            "\n\n请对【每条】发言分别执行抽取规则，输出 JSON：\n"
            '{"posts": [{"idx": 0, "mentions": [{"raw_name": "", "quote": "", "qty": ""}],'
            ' "account": {"position": "", "pnl": ""}}, ...]}\n'
            "其中 idx 必须对应上面的 [i]；某条没有提及任何标的时返回 \"mentions\":[]。")
    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    if not out:
        return None
    s = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL | re.I)
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except Exception:
        return None
    res = {}
    for p in (d.get("posts") or []):
        # 防御：模型偶发把某条帖子输出成裸字符串（2026-08-21 线上 '[提及] 生成失败'），跳过
        if not isinstance(p, dict):
            continue
        idx = p.get("idx")
        if isinstance(idx, int) and 0 <= idx < len(posts):
            res[idx] = p
    return res or None


# ---------------------------------------------------------------- 三道校验闸

def _squash(s):
    return re.sub(r"\s+", "", s or "")


def validate(payload, text):
    """三道闸拦幻觉。任何编造标的或改写原文的输出都会在此被拦下。"""
    # 防御：模型偶发返回非对象 / mentions 为字符串等（2026-08-21 线上 '[提及] 生成失败'）
    if not isinstance(payload, dict):
        return None
    mentions = payload.get("mentions")
    if not isinstance(mentions, list):
        mentions = []
    if len(mentions) > MAX_MENTIONS_PER_POST:
        print(f"[extractor] ⚠️ 单帖 {len(mentions)} 个标的，超阈值，整条丢弃")
        return None

    flat = _squash(text)
    ok = []
    for m in mentions:
        if not isinstance(m, dict):
            continue
        raw = (m.get("raw_name") or "").strip()
        quote = (m.get("quote") or "").strip()

        # 闸1：标的名必须真实出现在原文中
        if not raw or _squash(raw) not in flat:
            print(f"[extractor] ⛔ 闸1 拦下（标的不在原文）: {raw!r}")
            continue
        # 闸2：摘录必须是原文子串，且包含该标的
        if not quote or _squash(quote) not in flat or _squash(raw) not in _squash(quote):
            print(f"[extractor] ⛔ 闸2 拦下（摘录被改写）: {quote[:40]!r}")
            quote = _pick_sentence(text, raw)      # 用代码重新摘一句救回
            if _squash(raw) not in _squash(quote):
                continue
        # 闸3：数量格式不合法则置空（不丢弃整条）
        qty = _valid_qty(m.get("qty"))
        ok.append({"raw_name": raw, "quote": quote, "qty": qty})

    acc = payload.get("account")
    if not isinstance(acc, dict):   # 防御：account 偶发为字符串
        acc = {}
    account = {}
    for k in ("position", "pnl"):
        v = (acc.get(k) or "").strip()
        account[k] = v if v and _squash(v) in flat else ""
    return {"mentions": ok, "account": account}


# ---------------------------------------------------------------- 单帖处理

def extract_post(text, uid, use_ai=True, ai_payload=None):
    """处理单条原创正文，返回校验后的 {mentions, account}。

    ai_payload：批量模式传入已提取的 payload（跳过本单帖 LLM 调用）；为 None 时走单帖调用。
    """
    if not text:
        return {"mentions": [], "account": {"position": "", "pnl": ""}}
    result = None
    if use_ai:
        payload = ai_payload if ai_payload is not None else _ai_extract(text, uid)
        result = validate(payload, text)
    if result is None:
        result = fallback_extract(text)
        result["_source"] = "dict"
    else:
        result["_source"] = "ai"
        # AI 未识别到但词典命中的标的，补进来（互为补充，不互相覆盖）
        known = {normalize_symbol(m["raw_name"])[0] for m in result["mentions"]}
        for m in fallback_extract(text)["mentions"]:
            if normalize_symbol(m["raw_name"])[0] not in known:
                result["mentions"].append(m)
        acc_fb = _scan_account(text)
        for k in ("position", "pnl"):
            if not result["account"].get(k):
                result["account"][k] = acc_fb[k]
        # AI 漏抽数量时，用词典扫描的数量兜底补上（避免 AI 模式 again 漏掉 109手 这类）
        fb_mentions = fallback_extract(text)["mentions"]
        fb_qty = {normalize_symbol(m["raw_name"])[0]: m["qty"]
                  for m in fb_mentions if m.get("qty")}
        for m in result["mentions"]:
            if not m.get("qty"):
                q = fb_qty.get(normalize_symbol(m["raw_name"])[0])
                if q:
                    m["qty"] = q
    return result


# ---------------------------------------------------------------- 存储合并

def load_store(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("users") is not None:
            return d
    except Exception:
        pass
    return {"schema_version": SCHEMA_VERSION, "updated_at": "", "users": {}}


def _blank_account():
    return {"position": "", "position_at": None, "position_quote": "",
            "position_quoted": False,
            "pnl": "", "pnl_at": None, "pnl_quote": "", "pnl_quoted": False}


def _user_slot(store, uid, name):
    u = store["users"].setdefault(str(uid), {
        "name": name or str(uid),
        "account": _blank_account(),
        "symbols": {},
        "processed_max_id": 0,
    })
    if name:
        u["name"] = name
    u.setdefault("processed_max_id", 0)
    u.setdefault("symbols", {})
    u.setdefault("account", _blank_account())
    return u


def _apply_mention(slot, m, post):
    """主行覆盖 + 旧记录压入历史（最多 HISTORY_KEEP 条）。"""
    canon, hit = normalize_symbol(m["raw_name"])
    if not canon:
        return
    sym = slot["symbols"].get(canon)
    # 数量固定显示：新提取为空时，保留已读取的数量（用户手动/历史值不丢），非空才覆盖
    new_qty = m.get("qty", "")
    prev_qty = (sym or {}).get("latest", {}).get("qty", "") if sym else ""
    rec_qty = new_qty or prev_qty
    rec = {"at": post["created_at"], "post_id": post["id"],
           "raw_name": m["raw_name"], "quote": m["quote"], "qty": rec_qty}
    if sym is None:
        slot["symbols"][canon] = {
            "normalized": hit,
            "aliases_seen": [m["raw_name"]],
            "latest": rec,
            "history": [],
            "first_at": post["created_at"],
            "mention_count": 1,
        }
        return
    sym["history"].insert(0, sym["latest"])
    sym["history"] = sym["history"][:HISTORY_KEEP]
    sym["latest"] = rec
    sym["mention_count"] = sym.get("mention_count", 0) + 1
    sym["normalized"] = sym.get("normalized", False) or hit
    if m["raw_name"] not in sym["aliases_seen"]:
        sym["aliases_seen"].append(m["raw_name"])
    if not sym.get("first_at") or post["created_at"] < sym["first_at"]:
        sym["first_at"] = post["created_at"]


def update_mentions(users, store_path, use_ai=True, force_all=False):
    """增量合并入库。users 为 tracker.py 的用户结构列表。

    只处理 post_id > processed_max_id 的帖子，保证幂等；遍历必须按时间升序，
    否则同日多条提及会把旧的写成最新。
    """
    store = load_store(store_path)
    stats = {"posts": 0, "mentions": 0, "skipped": 0, "ai": 0, "dict": 0}

    for u in users:
        uid = str(u.get("user_id") or "")
        if not uid:
            continue
        slot = _user_slot(store, uid, u.get("name"))
        seen_max = 0 if force_all else slot.get("processed_max_id", 0)

        pool = {p["id"]: p for p in (u.get("recent_posts") or [])}
        pool.update({p["id"]: p for p in (u.get("posts") or [])})
        posts = sorted(pool.values(), key=lambda p: (p.get("created_at") or 0))

        # 先收集本用户新帖（post_id > seen_max）
        new_posts = []
        for p in posts:
            pid = p.get("id") or 0
            if pid <= seen_max:
                continue
            body = split_original(p.get("text", ""))
            if not body:
                stats["skipped"] += 1
                slot["processed_max_id"] = max(slot["processed_max_id"], pid)
                continue
            new_posts.append((p, body))

        # 批量 AI 提取：本用户全部新帖合并为一次 LLM 调用（2026-08-20 优化，
        # 原逐帖调用在新增 20 条时产生 20 次调用+429 限流）；失败整体回退词典。
        batch = None
        if use_ai and new_posts:
            batch = _ai_extract_batch([b for _, b in new_posts], uid)

        for i, (p, body) in enumerate(new_posts):
            pid = p.get("id") or 0
            ai_payload = (batch or {}).get(i) if batch else None
            res = extract_post(body, uid, use_ai=use_ai, ai_payload=ai_payload)
            stats[res.get("_source", "dict")] += 1
            stats["posts"] += 1

            for m in res["mentions"]:
                _apply_mention(slot, m, p)
                stats["mentions"] += 1

            acc = res["account"]
            for k in ("position", "pnl"):
                if acc.get(k):
                    slot["account"].update({
                        k: acc[k],
                        f"{k}_at": p["created_at"],
                        f"{k}_quote": body[:120],
                        f"{k}_quoted": False,
                    })

            # 原创段没写到的账户信息，从「本人被自己转引的旧发言」里补。
            # 只填补空位、不覆盖原创段结论，并标记 quoted 供页面显示「引述」。
            if not (acc.get("position") and acc.get("pnl")):
                self_q = split_self_quoted(p.get("text", ""), u.get("name"))
                if self_q:
                    acc_q = _scan_account(self_q)
                    for k in ("position", "pnl"):
                        if acc_q.get(k) and not slot["account"].get(k):
                            slot["account"].update({
                                k: acc_q[k],
                                f"{k}_at": p["created_at"],
                                f"{k}_quote": self_q[:120],
                                f"{k}_quoted": True,
                            })

            slot["processed_max_id"] = max(slot["processed_max_id"], pid)

    return store, stats


def save_store(store, path, ts):
    store["schema_version"] = SCHEMA_VERSION
    store["updated_at"] = ts
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
