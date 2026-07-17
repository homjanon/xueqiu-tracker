"""配置：从环境变量读取，缺失时用默认值。
模型调用按优先级走三级后端（均为原生多模态，图文通吃）：
  1) NVIDIA Qwen3.5-VL（免费）
  2) NVIDIA Kimi-K2.5（免费）
  3) 硅基流动 Qwen3.5-35B-A3B（便宜付费）
"""
import os

XUEQIU_USER_ID = os.getenv("XUEQIU_USER_ID", "6515752937")

# 三级后端链（按顺序尝试，首个有 Key 且成功的生效）
BACKENDS = [
    {
        "name": "nvidia-qwen3.5-vl",
        "base_url": os.getenv("PRIMARY_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": os.getenv("PRIMARY_MODEL", "qwen/qwen3.5-397b-a17b"),
    },
    {
        "name": "nvidia-kimi-k2.5",
        "base_url": os.getenv("FALLBACK1_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),  # 与主力同属 NVIDIA
        "model": os.getenv("FALLBACK1_MODEL", "moonshotai/kimi-k2.5"),
    },
    {
        "name": "siliconflow-qwen3.5-35b",
        "base_url": os.getenv("FALLBACK2_BASE_URL", "https://api.siliconflow.cn/v1"),
        "api_key": os.getenv("SILICONFLOW_API_KEY", ""),
        "model": os.getenv("FALLBACK2_MODEL", "Qwen/Qwen3.5-35B-A3B"),
    },
]

# 抓取参数
PAGES = int(os.getenv("PAGES", "2"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

# 输出目录
DATA_DIR = os.getenv("DATA_DIR", "data")
REPORT_DIR = os.getenv("REPORT_DIR", "reports")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
