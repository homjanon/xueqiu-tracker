"""大V投资画像：每日把新增发言幂等地喂给 LLM，持续修订 data/vip_profiles.json。

设计（2026-09-24，迁移 douban-tracker 楼主画像的防污染经验）：
- 5 维度锁定 schema：投资理念 / 选股与分析方法 / 交易与仓位习惯 /
  关注领域与常谈标的 / 风险态度与心理特质——只记**稳定特质**，
  严禁行情判断/具体点位/仓位数字/今日操作等时效性内容写入（否则永久污染）。
- 幂等消费：profile_max_id 游标独立于 state 去重游标（state 服务展示、
  抓取后即推进；画像游标只在 LLM 成功后推进）。待消费 = pending_posts
  （上次失败遗留）+ 本轮新增；LLM 失败/输出走样 → 画像不动、发言存
  pending 下轮自动重试，绝不丢失；无待消费 → 0 次 LLM 调用（同日重跑自动跳过）。
- 重写式修订：有新依据的维度输出整合历史要点与今日新依据后的完整描述，
  可独立阅读；无新依据的维度原样返回。
- evolution 同日幂等（同日重跑替换末行不追加），保留最近 60 条。
- LLM 复用 analyzer.call_multi 三级后端（Gemini 3 Flash → Agnes 2.5 → SenseNova），
  每用户每天最多 1 次调用，失败不影响主流程。
"""
import datetime
import json
import os
import re

from analyzer import call_multi
from config import USER_HINTS

PROFILE_FILE = os.getenv("PROFILE_FILE", "vip_profiles.json")  # 相对 data_dir

DIMS = ["投资理念", "选股与分析方法", "交易与仓位习惯",
        "关注领域与常谈标的", "风险态度与心理特质"]
EVO_KEEP = 60          # evolution 保留条数
PENDING_MAX = 50       # pending_posts 封顶（防异常膨胀）
MAX_CONSUME = 80       # 单次喂给 LLM 的发言上限（洪流日取最新 80 条，旧发言让位）
PROFILE_BUDGET = 60    # 本模块 LLM 总时限（秒），与全局 budget 口径一致

PROFILE_SCHEMA_HINT = ('{"profile": {"投资理念": "...", "选股与分析方法": "...", '
                       '"交易与仓位习惯": "...", "关注领域与常谈标的": "...", '
                       '"风险态度与心理特质": "..."}, "summary": "一句话总评（≤80字）"}')


def _profile_path(data_dir):
    return os.path.join(data_dir, PROFILE_FILE)


def load_profiles(data_dir):
    try:
        with open(_profile_path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("users"), dict):
            return d
    except Exception:
        pass
    return {"schema_version": 1, "users": {}}


def save_profiles(data_dir, prof, ts):
    prof["updated_at"] = ts
    try:
        with open(_profile_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[画像] ⚠️ 写入失败: {e}")
        return False


def _clean_think(s):
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL | re.IGNORECASE)


def _extract_json(content):
    """从模型输出提取 JSON 对象（剥思考块/围栏，截取首个 { 到末个 }）。"""
    if not content:
        return None
    s = _clean_think(content)
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _posts_block(consume):
    return "\n".join(f"- [{p.get('id', '?')}] {(p.get('text') or '').strip()}"
                     for p in consume if p.get("text"))


def _build_messages(entry, name, uid, consume):
    hint = USER_HINTS.get(uid, "")
    base_sys = ("你是投资研究分析师，长期跟踪雪球大V的公开发言，"
                "请依据发言原文提炼/修订该用户的投资画像。"
                "画像只记录**稳定特质**（投资理念、方法、交易习惯、能力圈、心理特质），"
                "严禁把行情判断、具体点位、仓位数字、今日操作等时效性内容写入画像——"
                "这类内容属于每日日报，写入会造成画像永久污染。"
                "每维度 ≤200 字、第三人称陈述、文字可独立阅读。"
                "严格输出 JSON，结构：" + PROFILE_SCHEMA_HINT)
    if hint:
        base_sys += "\n\n黑话提示（仅用于理解发言，不得照抄进画像）：\n" + hint
    if entry.get("profile"):
        cur = json.dumps(entry["profile"], ensure_ascii=False, indent=1)
        user = (f"用户「{name}」的现有画像：\n{cur}\n\n"
                f"今日新增发言：\n{_posts_block(consume)}\n\n"
                "请输出修订后的完整画像 JSON。规则：\n"
                "① 重写式修订：有新依据的维度，输出整合历史要点与今日新依据后的完整描述；\n"
                "② 无新依据的维度，原样返回现有内容（一字不改）；\n"
                "③ summary 按最新认知更新（≤80字）；\n"
                "④ 只输出 JSON，不要解释。")
    else:
        user = (f"用户「{name}」的近期发言：\n{_posts_block(consume)}\n\n"
                "请提炼初始投资画像（输出 JSON，不要解释）。")
    return [{"role": "system", "content": base_sys},
            {"role": "user", "content": user}]


def _apply(entry, parsed, consume, today):
    """校验 parsed 并写入 entry 副本。返回 (new_entry, n_updated) 或 (None, 0)。"""
    prof = parsed.get("profile") if isinstance(parsed, dict) else None
    if not isinstance(prof, dict):
        return None, 0
    new_profile = dict(entry.get("profile") or {})
    n = 0
    for dim in DIMS:                      # 锁定 schema：只认这 5 个键，防维度膨胀
        v = prof.get(dim)
        if isinstance(v, str) and v.strip():
            txt = v.strip()
            if txt != new_profile.get(dim):
                n += 1
            new_profile[dim] = txt
    if not new_profile:                   # 一个维度都没拿到 → 视为无效输出
        return None, 0
    new_entry = dict(entry)
    new_entry["profile"] = new_profile
    s = parsed.get("summary")
    if isinstance(s, str) and s.strip():
        new_entry["summary"] = s.strip()[:120]
    new_entry["profile_max_id"] = max([p.get("id") or 0 for p in consume] +
                                      [entry.get("profile_max_id", 0)])
    new_entry["last_profile_date"] = today
    new_entry["pending_posts"] = []
    # evolution 同日幂等：同日重跑替换末行，不重复累积
    dims_changed = [d for d in DIMS if prof.get(d)]
    verb = "初始画像" if not entry.get("profile") else "更新"
    line = f"{today}：{verb} {len(dims_changed)} 维度（消费发言 {len(consume)} 条）"
    evo = list(entry.get("evolution") or [])
    if evo and evo[-1].startswith(f"{today}："):
        evo[-1] = line
    else:
        evo.append(line)
    new_entry["evolution"] = evo[-EVO_KEEP:]
    return new_entry, n


def update_profiles(users, data_dir, today=None, ts=None):
    """幂等消费各用户新增发言，修订 data/vip_profiles.json。

    返回审计行列表；LLM 失败/无新增均不影响主流程。
    """
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    today = today or now.strftime("%Y-%m-%d")
    ts = ts or now.strftime("%Y-%m-%d %H:%M:%S")

    prof = load_profiles(data_dir)
    audit = []
    for u in users:
        uid = str(u.get("user_id", ""))
        name = u.get("name") or uid
        if not uid:
            continue
        entry = dict(prof["users"].get(uid) or {})
        entry.setdefault("name", name)
        entry.setdefault("profile_max_id", 0)
        entry.setdefault("pending_posts", [])
        # 待消费 = 上次失败遗留 + 本轮新增（画像游标之后），按 id 去重
        pending = [p for p in entry.get("pending_posts") or [] if isinstance(p, dict)]
        fresh = [p for p in (u.get("posts") or [])
                 if isinstance(p, dict) and (p.get("id") or 0) > entry.get("profile_max_id", 0)]
        seen, consume = set(), []
        for p in pending + fresh:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            consume.append(p)
        if not consume:
            continue                       # 幂等：无待消费 → 0 次 LLM 调用
        consume = sorted(consume, key=lambda p: p.get("id") or 0)[-MAX_CONSUME:]
        entry["name"] = name
        messages = _build_messages(entry, name, uid, consume)
        out = call_multi(messages, budget=PROFILE_BUDGET)
        parsed = _extract_json(out) if out else None
        new_entry, n = _apply(entry, parsed, consume, today) if parsed else (None, 0)
        if new_entry is not None:
            prof["users"][uid] = new_entry
            audit.append(f"{name}: 画像已修订（更新 {n} 维度 · 消费发言 {len(consume)} 条）")
        else:
            entry["pending_posts"] = consume[-PENDING_MAX:]
            prof["users"][uid] = entry
            audit.append(f"{name}: LLM 未产出有效画像，{len(consume)} 条发言转 pending 下轮重试")
    save_profiles(data_dir, prof, ts)
    return audit
