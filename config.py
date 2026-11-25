"""配置：从环境变量读取，缺失时用默认值。
模型调用按优先级走三级后端（均为原生多模态，图文通吃）：
  1) 商汤日日新 SenseNova deepseek-flash（= DeepSeek V4.1 Flash，SENSENOVA_API_KEY；2026-09-24 起首选：
     国内场景直连快、公测免费无配额压力，reasoning_effort=low 实测 2-3s 且 content 稳定非空。
     ⚠️ 2026-11-16 由 deepseek-v4-flash 升级为 deepseek-flash：商汤公告 DeepSeek V4 Flash
     于 2026-11-24 下线，要求在此前把 Model ID 换成 deepseek-flash）
  2) Agnes AI agnes-3.0-flash（AGNES_API_KEY，免费多模态，次选；2026-11-25 由 agnes-2.5-flash
     升级——官方公告 2.5 于 2026-11-30 下线。3.0 为推理模型，extra 固定关思考
     enable_thinking=false，防思考挤占可见输出、拖长耗时）
  3) Google Gemini 3 Flash（GEMINI_API_KEY，gemini-3-flash-preview；2026-09-24 晚间由 3.8 换回
     ——3.8 免费档 503 连续拥堵，换回 09-16 验证稳定的 3-flash-preview 作确定性兜底）
支持多用户（逗号分隔）；USER_HINTS 为各用户专属黑话词典（注入 LLM 提示）。
"""
import os

XUEQIU_USER_IDS = [x.strip() for x in
                   os.getenv("XUEQIU_USER_IDS", "6515752937,1821992043").split(",") if x.strip()]

# 各用户专属黑话/习惯提示（注入 LLM，提升买/卖/持有识别准确率）
USER_HINTS = {
    "1821992043": """【该用户黑话提示，请据此正确解读】
- "mnp" = 实盘操作（真实的买卖动作）
- "羊毛" = 做差价/做T（通常在招商银行与宁波银行之间来回做，因两者长期走势同步）
- "大波" = 宁波银行（代码 SZ002142）
- "小招"/"小昭" = 招商银行（代码 SH600036）
- "进货" = 买入
- "招行"/"CMBank" = 招商银行
- 该用户常交易标的：招商银行(招行/小招)、宁波银行(大波/宁波行)、五粮液、中国平安
请结合谐音、昵称、常理合理推测其是否有实盘操作（买入/卖出/加仓/减仓/持有）；只要有真实买卖动作就标出 action，不要因用了黑话就忽略。stocks 写用户原文叫法即可（如 大波、小招、招行），无需转成官方名或代码。""",
}

BACKENDS = [
    {
        # ① 首选：商汤日日新 SenseNova deepseek-flash（= DeepSeek V4.1 Flash；2026-09-24 由 ③ 上移。
        #   动因：国内股市场景，商汤国内直连快、公测免费无配额压力；
        #   reasoning_effort=low 轻思考实测 12s→2.6s 且 content 稳定非空（参考 qiugecaozuo 用法）；
        #   max_tokens 需给足，否则思考链抢占预算致 content 截断/为空）
        #   ⚠️ 2026-11-16：Model ID 由 deepseek-v4-flash 换为 deepseek-flash（V4 Flash 2026-11-24 下线）。
        #     注意 ID 是 `deepseek-flash`（不带版本号），不是 `deepseek-v4.1-flash`；
        #     商汤模型表里新旧两个 ID 曾并列，填旧 ID 不报错、只会静默继续跑旧模型。
        "name": "sensenova-deepseek-flash",
        "base_url": os.getenv("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
        "api_key": os.getenv("SENSENOVA_API_KEY", ""),
        "model": os.getenv("SENSENOVA_MODEL", "deepseek-flash"),
        "timeout": int(os.getenv("SENSENOVA_TIMEOUT", "30")),
        "max_tokens": 8000,
        "extra": {"reasoning_effort": "low"},
    },
    {
        # ② 次选：Agnes AI agnes-3.0-flash（免费多模态；2026-11-25 由 agnes-2.5-flash 升级，
        #   动因：官方公告 agnes-2.5-flash 于 2026-11-30 下线，默认对话模型切为
        #   agnes-3.0-flash（512K 上下文 / 最大输出 65,536，当前免费）。
        #   ⚠️ 3.0 是推理模型：不关思考会先消费 output tokens、长输入耗时不可控
        #   （参考仓曾在 74K 字符输入下 90s 超时），故 extra 发官方 chat template 的
        #   chat_template_kwargs.enable_thinking=false 关闭内部思考。
        #   2.5 曾实测返回 200 但 content 空，故仍居次选位；_post 已兼容
        #   content / reasoning_content，3.0 答案若落在 reasoning 系字段也不会被误判为空。
        "name": "agnes-3.0-flash",
        "base_url": os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"),
        "api_key": os.getenv("AGNES_API_KEY", ""),
        "model": os.getenv("AGNES_MODEL", "agnes-3.0-flash"),
        "timeout": int(os.getenv("AGNES_TIMEOUT", "30")),
        "extra": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    {
        # ③ 兜底：Google Gemini 3 Flash（2026-09-24 晚间由 3.8 换回——
        #   3.8 免费档 503「high demand」连续拥堵（run122/123 连续观察到），
        #   换回 09-16 验证稳定的 3-flash-preview 作确定性兜底。
        #   ⚠️ 该模型名必须带 -preview 后缀（无后缀会 404）；
        #   3 系无法关闭思考，_extract_text 已兼容 <think>/reasoning_content）
        "name": "gemini-3-flash",
        "base_url": os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        "api_key": os.getenv("GEMINI_API_KEY", ""),
        "model": os.getenv("GEMINI_MODEL", "gemini-3-flash-preview"),
        "timeout": int(os.getenv("GEMINI_TIMEOUT", "60")),
    },
]


# 全局默认超时（各后端可用 BACKENDS[].timeout 覆盖；2026-08-20 从 150 收紧到 60，防叠加拖垮 job）
TIMEOUT = int(os.getenv("TIMEOUT", "60"))

PAGES = int(os.getenv("PAGES", "2"))

# 无新增发言时，每人保留最近多少条发言作为网站兜底展示
RECENT_N = int(os.getenv("RECENT_N", "10"))

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

DATA_DIR = os.getenv("DATA_DIR", "data")
REPORT_DIR = os.getenv("REPORT_DIR", "reports")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# 每日报告只保留最近多少天（2026-08-20 用户要求，默认 90 日）
KEEP_REPORT_DAYS = int(os.getenv("KEEP_REPORT_DAYS", "90"))

# ============ 标的提及追踪（mentions.json） ============

# 标的别名词典：规范名 -> 该标的在发言中可能出现的各种叫法
# 归一化是「覆盖式更新」的命门：若「酒家」与「广州酒家」不归一，会变成两行，覆盖失效。
# 匹配规则：最长优先；未命中词典的标的原样入库并在页面标「?」角标，人工反馈后补录。
SYMBOL_ALIAS = {
    "招商银行": ["招行", "小招", "小昭", "cmbank", "cmb", "600036"],
    "宁波银行": ["大波", "宁波行", "002142"],
    "建设银行": ["建行", "601939"],
    "工商银行": ["工行", "601398"],
    "农业银行": ["农行", "601288"],
    "中国银行": ["中行"],
    "交通银行": ["交行"],
    "兴业银行": ["兴业"],
    "平安银行": ["平银"],
    "广州酒家": ["酒家", "广酒", "603043"],
    "青岛港": ["青岛港h股", "青岛港h", "青岛港H股", "06198", "601298"],
    "顺丰控股": ["顺丰", "002352"],
    "马应龙": ["600993"],
    "五粮液": ["000858"],
    "中国平安": ["601318"],
    "安琪酵母": ["安琪", "600298"],
    "贵州茅台": ["茅台", "600519"],
    "长江电力": ["长电", "600900"],
    "银行ETF": ["银行etf", "512800"],
}

# 单条发言最多接受多少个标的，超出视为模型异常输出，整条丢弃
MAX_MENTIONS_PER_POST = 8

# 主行之外保留多少条历史记录
HISTORY_KEEP = 3

MENTIONS_FILE = os.getenv("MENTIONS_FILE", "mentions.json")

# 数量人工锁定名单（2026-10-04）：键 = (user_id, 规范标的名)
#
# 动因：ice_招行谷子地对招商银行几乎只买不卖，其发言里的「数量」多为**增量**
#   （如「周一周二 mnp 在这里抢了 1500 股」= 在底仓上又加了 1500 股），而抽取层
#   无法稳定区分 增量 / 存量 / 做 T —— 09-30 起把 1500股 当成持仓总量，覆盖掉了
#   人工维护的 876000股（原合并逻辑「新值优先」，任何抽到的数字都会盖掉旧值）。
# 语义：命中后该标的的 qty **只保留已存值**，抽取结果一律不覆盖；数值由人工维护
#   （直接改 data/mentions.json 的 latest.qty，无需改代码），输出带 qty_locked=true，
#   看板据此显示「🔒 人工」角标。
# 增删标的：在此增删一行即可；未列入的标的完全走原逻辑（新值覆盖 / 空值沿用）。
QTY_LOCKED = {
    ("1821992043", "招商银行"),
}
