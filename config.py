"""配置：从环境变量读取，缺失时用默认值。
模型调用按优先级走三级后端（均为原生多模态，图文通吃）：
  1) Agnes AI agnes-2.0-flash（免费多模态，复用 douban-tracker 配置）
  2) NVIDIA GLM-5.2（z-ai/glm-5.2，免费，参考 portfolio 仓调用方式）
  3) 商汤日日新 SenseNova 6.7 Flash-Lite（免费，Token Plan 限时免费）
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
        # ① NVIDIA GLM-5.2（免费；2026-08-20 实测 0.9s 最快最稳，升为第一优先）
        "name": "nvidia-glm-5.2",
        "base_url": os.getenv("PRIMARY_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": os.getenv("PRIMARY_MODEL", "z-ai/glm-5.2"),
        "timeout": int(os.getenv("PRIMARY_TIMEOUT", "30")),
    },
    {
        # ② Agnes AI agnes-2.0-flash（免费多模态；8-20 实测返回 200 但 content 空 → 不兜底，降为第二）
        "name": "agnes-2.0-flash",
        "base_url": os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"),
        "api_key": os.getenv("AGNES_API_KEY", ""),
        "model": os.getenv("AGNES_MODEL", "agnes-2.0-flash"),
        "timeout": int(os.getenv("AGNES_TIMEOUT", "30")),
    },
    {
        # ③ 兜底：商汤日日新 SenseNova 6.8 Flash-Lite（免费；6.7 即将下线，2026-08-20 换版）
        "name": "sensenova-6.8-flash-lite",
        "base_url": os.getenv("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
        "api_key": os.getenv("SENSENOVA_API_KEY", ""),
        "model": os.getenv("SENSENOVA_MODEL", "sensenova-6.8-flash-lite"),
        "timeout": int(os.getenv("SENSENOVA_TIMEOUT", "30")),
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
