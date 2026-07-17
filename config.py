"""配置：从环境变量读取，缺失时用默认值。
模型调用按优先级走三级后端（均为原生多模态，图文通吃）：
  1) NVIDIA Qwen3.5-122B-A10B（免费，原生VLM，122B总参/10B激活，比397B更快更稳）
  2) NVIDIA Kimi-K2.5（免费，走 build.nvidia.com 专属 endpoint）
  3) 硅基流动 Qwen3.5-35B-A3B（便宜付费，已验证可用）
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
        "name": "nvidia-qwen3.5-122b",
        # 原生多模态视觉模型（122B总参/10B激活），比 397B 更小更快，免费档更稳
        "base_url": os.getenv("PRIMARY_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": os.getenv("PRIMARY_MODEL", "qwen/qwen3.5-122b-a10b"),
        "timeout": int(os.getenv("PRIMARY_TIMEOUT", "120")),
    },
    {
        "name": "nvidia-kimi-k2.5",
        # Kimi-K2.5 不在统一网关（会 404），须走 build.nvidia.com 专属 endpoint
        "base_url": os.getenv("FALLBACK1_BASE_URL",
                              "https://ai.api.nvidia.com/v1/nim/moonshotai/kimi-k2.5/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": os.getenv("FALLBACK1_MODEL", "moonshotai/kimi-k2.5"),
        "timeout": int(os.getenv("FALLBACK1_TIMEOUT", "150")),
    },
    {
        "name": "siliconflow-qwen3.5-35b",
        "base_url": os.getenv("FALLBACK2_BASE_URL", "https://api.siliconflow.cn/v1"),
        "api_key": os.getenv("SILICONFLOW_API_KEY", ""),
        "model": os.getenv("FALLBACK2_MODEL", "Qwen/Qwen3.5-35B-A3B"),
        "timeout": int(os.getenv("FALLBACK2_TIMEOUT", "90")),
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
