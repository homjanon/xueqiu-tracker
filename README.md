# xueqiu-vip-tracker

自动跟踪指定雪球大V的每日发言，识别其中**明确的买入/卖出/加仓/减仓/持有**操作，并读取其**截图**（价格/数量/K线），输出机器可读数据供外部网站直接读取。

## 工作原理
1. **Playwright 真实浏览器**加载雪球首页，执行其阿里云 WAF 的 JS 挑战，拿到 `xq_a_token` Cookie（纯 HTTP 无法绕过此 WAF）。
2. 带 Cookie 调用雪球时间线 JSON 接口 `statuses/user_timeline.json`，抓取动态。
3. 清洗 HTML、按 `last_post_id` 去重，仅处理新增发言。
4. **信号识别**走三级后端链（均为原生多模态，图文通吃），首个可用即生效：
   - ① NVIDIA **Qwen3.5-VL**（`qwen/qwen3.5-397b-a17b`）— 免费
   - ② NVIDIA **Kimi-K2.5**（`moonshotai/kimi-k2.5`）— 免费
   - ③ 硅基流动 **Qwen3.5-35B-A3B**（`Qwen/Qwen3.5-35B-A3B`）— 便宜付费
   - 无 Key 时回退关键词启发式（仅文字）。
5. 对**带图发言**：下载截图（带 Referer 规避防盗链）→ 压缩 → 送视觉模型，抽取 `action / stocks / price / quantity / trend`。
6. 输出：
   - `data/latest.json` —— **网站读取此文件**（机器可读，含新增发言 + 文字/图片信号）
   - `reports/YYYY-MM-DD.md` —— 人读简报
   - `state.json` —— 增量去重状态（由工作流提交回仓库）

## 本地运行
```bash
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env   # 填 NVIDIA_API_KEY / SILICONFLOW_API_KEY
python tracker.py
```

## GitHub Actions（推荐）
1. 把仓库推到 GitHub。
2. `Settings → Secrets → Actions` 添加：`XUEQIU_USER_ID`、`NVIDIA_API_KEY`、`SILICONFLOW_API_KEY`。
3. 工作流每天**北京时间 22:00** 自动运行（亦可在 Actions 页手动触发），运行后自动提交 `data/`、`reports/`、`state.json`。

## 你的网站如何读取
直接拉取仓库里的 `data/latest.json`（如 `https://raw.githubusercontent.com/<你>/xueqiu-tracker/main/data/latest.json`），解析 `posts` 与 `text_signals`/`vision_signals` 字段即可。

## 注意事项
- 雪球有 WAF，必须浏览器抓取；若 GitHub 数据中心 IP 被限流，可改用 **Self-hosted Runner**（常开机器）或代理。
- 密钥只存在于 GitHub Secrets，永不进源码；本仓库不含任何密钥。
- 启发式（无 LLM Key）仅作兜底，置信度低；接入视觉模型后质量明显提升。
- `created_at` 为毫秒时间戳；发言含 `//@`（转发）与 `回复@`（评论），已保留原文供判断。
