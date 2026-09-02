# xueqiu-vip-tracker

自动跟踪**多个**x大V的每日发言，输出两类机器可读数据供外部网站直接读取：

1. **每日讨论归纳**（`latest.json` 的 `daily_summary`）：LLM 中性归纳每人讨论了什么（每句 40-60 字），重点抓取点名的具体标的；
2. **标的提及追踪**（`mentions.json`，本仓核心新增）：自动摘录每人点名的股票/ETF、对应**原话片段**、数量、以及账户**仓位 / 盈亏**，由你自己看原话判断操作，**不判断买卖方向**。

## 工作原理
1. **Playwright 真实浏览器**加载xx首页，执行其阿里云 WAF 的 JS 挑战，拿到 `xq_a_token` Cookie（纯 HTTP 无法绕过此 WAF）。
2. 带 Cookie 调用xx时间线 JSON 接口 `statuses/user_timeline.json`，按 `XUEQIU_USER_IDS`（逗号分隔）逐个抓取动态。
3. 清洗 HTML、按各用户 `last_post_id` 去重，仅处理新增发言。
4. **每日讨论归纳**（`analyzer.daily_summary`）：每位用户各自调用 LLM，把其发言**中性归纳成一句 40-60 字**的短评；**重点抓取用户点名的具体标的（股票/ETF，勿以「消费/港口/券商」等泛称带过）**，可如实转述原文明确表达的动作（如「加仓XX」「出了XX」），但**不替用户推断未明说的操作**（不自行下「持有XX」结论）；某人当日无发言则显示「暂未发言」。三级后端链首个可用即生效：
   - ① NVIDIA **GLM-5.2**（`z-ai/glm-5.2`，参考 portfolio 仓调用方式）— 免费，实测最快最稳，第一优先
   - ② **Agnes AI agnes-2.0-flash**（`agnes-2.0-flash`，复用 douban-tracker 配置）— 免费（曾实测返回 200 但 content 空，降为第二）
   - ③ 商汤日日新 **SenseNova deepseek-v4-flash**（`deepseek-v4-flash`，`reasoning_effort=low` 轻思考 + `max_tokens=8000`，实测 2-3s 返回、content 稳定非空；用法对齐 qiugecaozuo 仓）— 免费兜底
   - 无 Key / 全部失败时回退：取该用户最新发言原文前段作摘录（不代码层截断，长度由提示词约束）。
5. 黑话提示 `USER_HINTS`（如 谷子地 的 mnp/大波/招行 等）作为轻量上下文注入，帮 LLM 读懂讨论，但归纳重点仍是抓取用户点名的具体标的。

## 设计取舍
- **不做交易信号提取**：此前尝试过 LLM/启发式判断买/卖/持仓并映射股票代码，但昵称映射、未标注标的、把提及误判为持有等问题反复出现。改为只做**中性归纳**，交易操作由你自行判断。
- 不强制 `response_format: json_object`（会迫使推理模型吐 `{reasoning:…}`）；用 `_extract_text` 鲁棒提取纯文本（兼容裸文本 / `{"summary":...}` / 围栏 / `<think>`块），**不再代码层截断**，长度交由提示词约束（40-60 字）。
- `text_signals` / `vision_signals` 字段**保留为空数组**（向后兼容网站），主信息为 `daily_summary` + 原始 `posts`。
7. 输出：
   - `data/latest.json` —— **网站读取此文件**（每日讨论归纳 + 原始发言，双结构见下）
   - `data/mentions.json` —— **标的提及追踪**（见下「标的提及追踪」一节），cmb-tracker 的雪球大V板块直接读取此文件渲染持仓表
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


## 标的提及追踪（mentions.json）

在「中性归纳」之上额外做**纯摘录**——只从原文里挖出「点名标的 + 原话 + 数量 + 账户仓位/盈亏」，**不做任何买卖方向判断**。人类看原话即知操作。该文件由 cmb-tracker 的雪球大V板块每日拉取渲染。

**抽取规则**
- **标的**：经 `config.SYMBOL_ALIAS` 别名词典归一（如「招行/小招/cmb」→ 招商银行），同一标的再次被点名则覆盖主行，旧值沉入 `history`（保留最近 3 条）。
- **数量**：仅在关键词附近 15 字内匹配正则（如「109手」「半仓」），无则留空。**关键修复**：某次抓取的新帖未提及数量（`qty` 为空）时，**沿用上一次已读取的值**，不再被空值覆盖成「—」（例如五粮液已读到 109手，后续空帖不会清空它）。如需改写数量，按原文摘录手动更新即可。
- **账户仓位 / 盈亏**：取帖子首个命中；盈亏须带「账户级标记」（带日期 / 年度区间 / 账户词如大盘·上证·总）才采信，避免把个股盈亏错算成账户盈亏。自引段（`//@本人:`）仅回填账户信息，不参与标的抽取。
- **三道校验门**：标的名与操作原文必须是原文子串、数量必须匹配正则，拦截幻觉。
- **`//@` 转发引用段已剔除**：不会把网友的持仓算到大V头上。
- **兜底**：无 LLM Key 时走字典扫描，照常产出（仅可能少抽定性描述）。
- **批量 LLM 抽取**：每用户新帖合并为一次批量调用（20 帖→1 次，2026-08-20 优化，大幅减少调用次数与 NVIDIA 429 限流）；LLM 返回结构走样（`posts` 混入裸字符串 / `account` 为字符串 / `mentions` 非列表）会由防御层跳过或置空，不再抛异常拖垮整个提及模块（2026-08-21 加固，含输入 str/dict 双形态兼容）。

**schema（节选）**
```jsonc
{
  "schema_version": 1,
  "updated_at": "2026-08-02 17:16:49",
  "users": {
    "<user_id>": {
      "name": "紫金陈",
      "account": {
        "position": "买到55%的仓位",            // 账户仓位描述
        "position_at": 1782874657000,          // 命中发言时间戳(ms)
        "position_quoted": true,               // 是否来自 //@ 自引段
        "position_quote": "……我买到55%的仓位……", // 原话
        "pnl": "今年收益率已经跑过上证",         // 账户整体盈亏（可定性）
        "pnl_at": 1785301547000,
        "pnl_quoted": false,
        "pnl_quote": "……"
      },
      "symbols": {                              // 对象：canonical name 为键，天然去重/覆盖
        "顺丰控股": {
          "normalized": true,
          "aliases_seen": ["顺丰"],
          "latest": { "at": 1784082663000, "post_id": 400235511,
                      "raw_name": "顺丰", "quote": "保本出了酒家和顺丰", "qty": "" },
          "history": [ /* 最近 3 条旧记录，同结构 */ ],
          "first_at": 1783388766000,           // 首次点名时间
          "mention_count": 2                    // 累计被点名次数
        }
      },
      "processed_max_id": 402640556             // 已处理到的最大 post_id（增量游标）
    }
  }
}
```
> 累计增量：每次运行只处理 `latest.json` 中 `processed_max_id` 之后的新发言，合并进既有 `mentions.json`，**绝不重建**，故历史与首次点名时间得以保留。

## GitHub Actions（推荐）
1. 把仓库推到 GitHub。
2. `Settings → Secrets → Actions` 添加：`XUEQIU_USER_IDS`、`NVIDIA_API_KEY`、`AGNES_API_KEY`、`SENSENOVA_API_KEY`。
   （`XUEQIU_USER_IDS` 形如 `6515752937,1821992043`）
3. 工作流每天**北京时间 14:30** 由 Cloudflare Worker（qdii-dispatch）触发（亦可在 Actions 页手动触发），运行后自动提交 `data/`、`reports/`、`state.json`。

