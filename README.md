# xueqiu-vip-tracker

自动跟踪**多个**x大V的每日发言，由 LLM **中性归纳每人讨论了什么**（每句 40-60 字），输出机器可读数据供外部网站直接读取。**重点抓取用户点名的具体标的**，操作由你自己看归纳来判断。

## 工作原理
1. **Playwright 真实浏览器**加载xx首页，执行其阿里云 WAF 的 JS 挑战，拿到 `xq_a_token` Cookie（纯 HTTP 无法绕过此 WAF）。
2. 带 Cookie 调用xx时间线 JSON 接口 `statuses/user_timeline.json`，按 `XUEQIU_USER_IDS`（逗号分隔）逐个抓取动态。
3. 清洗 HTML、按各用户 `last_post_id` 去重，仅处理新增发言。
4. **每日讨论归纳**（`analyzer.daily_summary`）：每位用户各自调用 LLM，把其发言**中性归纳成一句 40-60 字**的短评；**重点抓取用户点名的具体标的（股票/ETF，勿以「消费/港口/券商」等泛称带过）**，可如实转述原文明确表达的动作（如「加仓XX」「出了XX」），但**不替用户推断未明说的操作**（不自行下「持有XX」结论）；某人当日无发言则显示「暂未发言」。三级后端链首个可用即生效：
   - ① **Agnes AI agnes-2.0-flash**（`agnes-2.0-flash`，复用 douban-tracker 配置）— 免费
   - ② NVIDIA **Qwen3.5-122B-A10B**（`qwen/qwen3.5-122b-a10b`，比397B更快）— 免费
   - ③ 商汤日日新 **SenseNova 6.7 Flash-Lite**（`sensenova-6.7-flash-lite`，Token Plan 限时免费）— 免费兜底
   - 无 Key / 全部失败时回退：取该用户最新发言原文前段作摘录（不代码层截断，长度由提示词约束）。
5. 黑话提示 `USER_HINTS`（如 谷子地 的 mnp/大波/招行 等）作为轻量上下文注入，帮 LLM 读懂讨论，但归纳重点仍是抓取用户点名的具体标的。

## 设计取舍
- **不做交易信号提取**：此前尝试过 LLM/启发式判断买/卖/持仓并映射股票代码，但昵称映射、未标注标的、把提及误判为持有等问题反复出现。改为只做**中性归纳**，交易操作由你自行判断。
- 不强制 `response_format: json_object`（会迫使推理模型吐 `{reasoning:…}`）；用 `_extract_text` 鲁棒提取纯文本（兼容裸文本 / `{"summary":...}` / 围栏 / `<think>`块），**不再代码层截断**，长度交由提示词约束（40-60 字）。
- `text_signals` / `vision_signals` 字段**保留为空数组**（向后兼容网站），主信息为 `daily_summary` + 原始 `posts`。
7. 输出：
   - `data/latest.json` —— **网站读取此文件**，采用「顶层合并 + `users[]` 明细」双结构（见下）
   - `reports/YYYY-MM-DD.md` —— 人读简报
   - `state.json` —— 多用户增量去重状态（由工作流提交回仓库）

## `data/latest.json` 结构
```jsonc
{
  "fetched_at": "2026-07-17 12:00:00",
  "daily_summary": "紫金陈：聚焦安琪酵母、东鹏饮料、鱼跃医疗等消费老登股，讨论回调布局与可转债风险\nice_招行谷子地：围绕招商银行、宁波银行做利差与打新，关注银行ETF与红利低波",
  "user_count": 2,
  "new_count": 10,
  "text_signal_count": 0,
  "vision_signal_count": 0,
  // —— 顶层合并：老网站零改动可直接读 ——
  "posts": [ /* 所有用户新增发言合并 */ ],
  "text_signals": [],   // 已不再提取交易信号，保留空数组向后兼容
  "vision_signals": [],
  // —— 每用户明细 ——
  "users": [
    {
      "user_id": "6xxxx", "name": "xxx",
      "new_count": 5, "text_signal_count": 0, "vision_signal_count": 0,
      "posts": [...], "text_signals": [], "vision_signals": []
    },
    { "user_id": "1xxx", "name": "x2xx", ... }
  ]
}
```
> 网站直接读 `daily_summary`（每人一句，换行分隔）即可获得当日概览；原始发言见 `posts` / `users[].posts`。`text_signals`/`vision_signals` 固定为空数组，仅作向后兼容预留。


## GitHub Actions（推荐）
1. 把仓库推到 GitHub。
2. `Settings → Secrets → Actions` 添加：`XUEQIU_USER_IDS`、`NVIDIA_API_KEY`、`AGNES_API_KEY`、`SENSENOVA_API_KEY`。
   （`XUEQIU_USER_IDS` 形如 `6515752937,1821992043`）
3. 工作流每天**北京时间 12:00** 自动运行（亦可在 Actions 页手动触发），运行后自动提交 `data/`、`reports/`、`state.json`。

