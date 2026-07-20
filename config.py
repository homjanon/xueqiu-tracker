"""配置：从环境变量读取，缺失时用默认值。
模型调用按优先级走三级后端（均为原生多模态，图文通吃）：
  1) Agnes AI agnes-2.0-flash（免费多模态，复用 douban-tracker 配置）
  2) NVIDIA Qwen3.5-122B-A10B（免费，原生VLM，122B总参/10B激活）
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
        # ① Agnes AI agnes-2.0-flash（免费多模态，复用 douban-tracker 配置）
        "name": "agnes-2.0-flash",
        "base_url": os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"),
        "api_key": os.getenv("AGNES_API_KEY", ""),
        "model": os.getenv("AGNES_MODEL", "agnes-2.0-flash"),
        "timeout": int(os.getenv("AGNES_TIMEOUT", "120")),
    },
    {
        # ② NVIDIA Qwen3.5-122B-A10B（免费，原生VLM，122B总参/10B激活）
        "name": "nvidia-qwen3.5-122b",
        "base_url": os.getenv("PRIMARY_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": os.getenv("PRIMARY_MODEL", "qwen/qwen3.5-122b-a10b"),
        "timeout": int(os.getenv("PRIMARY_TIMEOUT", "120")),
    },
    {
        # ③ 兜底：商汤日日新 SenseNova 6.7 Flash-Lite（免费，Token Plan 限时免费）
        "name": "sensenova-6.7-flash-lite",
        "base_url": os.getenv("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
        "api_key": os.getenv("SENSENOVA_API_KEY", ""),
        "model": os.getenv("SENSENOVA_MODEL", "sensenova-6.7-flash-lite"),
        "timeout": int(os.getenv("SENSENOVA_TIMEOUT", "120")),
    },
]

# 全局默认超时（各后端可用 BACKENDS[].timeout 覆盖）
TIMEOUT = int(os.getenv("TIMEOUT", "150"))

PAGES = int(os.getenv("PAGES", "2"))

# 无新增发言时，每人保留最近多少条发言作为网站兜底展示
RECENT_N = int(os.getenv("RECENT_N", "10"))

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

DATA_DIR = os.getenv("DATA_DIR", "data")
REPORT_DIR = os.getenv("REPORT_DIR", "reports")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
