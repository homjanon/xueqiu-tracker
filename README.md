# xueqiu-vip-tracker

自动跟踪**多个**雪球大V的每日发言，识别其中**明确的买入/卖出/加仓/减仓/持有**操作，并读取其**截图**（价格/数量/K线），输出机器可读数据供外部网站直接读取。

## 工作原理
1. **Playwright 真实浏览器**加载雪球首页，执行其阿里云 WAF 的 JS 挑战，拿到 `xq_a_token` Cookie（纯 HTTP 无法绕过此 WAF）。
2. 带 Cookie 调用雪球时间线 JSON 接口 `statuses/user_timeline.json`，按 `XUEQIU_USER_IDS`（逗号分隔）逐个抓取动态。
3. 清洗 HTML、按各用户 `last_post_id` 去重，仅处理新增发言。
4. **信号识别**走三级后端链（均为原生多模态，图文通吃），首个可用即生效：
   - ① NVIDIA **Qwen3.5-VL**（`qwen/qwen3.5-397b-a17b`）— 免费
   - ② NVIDIA **Kimi-K2.5**（`moonshotai/kimi-k2.5`）— 免费
   - ③ 硅基流动 **Qwen3.5-35B-A3B**（`Qwen/Qwen3.5-35B-A3B`）— 便宜付费
   - 无 Key 时回退关键词启发式（仅文字）。
5. 对**带图发言**：下载截图（带 Referer 规避防盗链）→ 压缩 → 送视觉模型，抽取 `action / stocks / price / quantity / trend`。
6. 跨用户合并生成**「每日一句话总结」**（`daily_summary`），由 LLM 浓缩全天重点操作，失败回退启发式拼接。
7. 输出：
   - `data/latest.json` —— **网站读取此文件**，采用「顶层合并 + `users[]` 明细」双结构（见下）
   - `reports/YYYY-MM-DD.md` —— 人读简报
   - `state.json` —— 多用户增量去重状态（由工作流提交回仓库）

## `data/latest.json` 结构
```jsonc
{
  "fetched_at": "2026-07-17 12:00:00",
  "daily_summary": "今天紫金陈加仓了青岛港，谷子地做了招行/宁波银行的羊毛差价……",
  "user_count": 2,
  "new_count": 10,
  "text_signal_count": 5,
  "vision_signal_count": 1,
  // —— 顶层合并：老网站零改动可直接读 ——
  "posts": [ /* 所有用户新增发言合并 */ ],
  "text_signals": [ /* 所有用户文字信号合并（含 _user 字段标注来源） */ ],
  "vision_signals": [ /* 所有用户图片信号合并（含 _user 字段标注来源） */ ],
  // —— 每用户明细 ——
  "users": [
    {
      "user_id": "6515752937", "name": "紫金陈",
      "new_count": 5, "text_signal_count": 3, "vision_signal_count": 1,
      "posts": [...], "text_signals": [...], "vision_signals": [...]
    },
    { "user_id": "1821992043", "name": "ice招行谷子地", ... }
  ]
}
```
> 网站若只需一句话概览，直接读 `daily_summary` 即可；若需明细，可遍历 `users[]` 或顶层合并字段。

## 黑话词典（按用户注入 LLM）
在 `config.py` 的 `USER_HINTS` 中按 `user_id` 配置专属黑话，识别时注入提示，避免把实盘操作当成闲聊漏掉。示例（谷子地）：
- `mnp` = 实盘操作（真实买卖）
- `羊毛` = 做差价/做T（多在招商银行与宁波银行之间）
- `大波` = 宁波银行（SZ002142）｜ `小招`/`小昭` = 招商银行（SH600036）
- `进货` = 买入

## 本地运行
```bash
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env   # 填 XUEQIU_USER_IDS / NVIDIA_API_KEY / SILICONFLOW_API_KEY
python tracker.py
```

## GitHub Actions（推荐）
1. 把仓库推到 GitHub。
2. `Settings → Secrets → Actions` 添加：`XUEQIU_USER_IDS`、`NVIDIA_API_KEY`、`SILICONFLOW_API_KEY`。
   （`XUEQIU_USER_IDS` 形如 `6515752937,1821992043`）
3. 工作流每天**北京时间 12:00** 自动运行（亦可在 Actions 页手动触发），运行后自动提交 `data/`、`reports/`、`state.json`。

## 你的网站如何读取
直接拉取仓库里的 `data/latest.json`（如 `https://raw.githubusercontent.com/<你>/xueqiu-tracker/main/data/latest.json`）：
- 只要一句概览 → 读 `daily_summary`；
- 要明细 → 遍历 `users[]`，或读顶层合并的 `posts` / `text_signals` / `vision_signals`。

## 注意事项
- 雪球有 WAF，必须浏览器抓取；若 GitHub 数据中心 IP 被限流，可改用 **Self-hosted Runner**（常开机器）或代理。
- 密钥只存在于 GitHub Secrets，永不进源码；本仓库不含任何密钥。
- 启发式（无 LLM Key）仅作兜底，置信度低；接入视觉模型后质量明显提升。
- `created_at` 为毫秒时间戳；发言含 `//@`（转发）与 `回复@`（评论），已保留原文供判断。
