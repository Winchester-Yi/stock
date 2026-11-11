# 批量研究命令与数据资格

此入口只采集、计算和输出精简证据，不启动模型、下单、保存人工报告或写 `analysis_runs`。最终方向仍由当前交互会话综合，按 [快速报告模式](report-format.md) 交付。

## 一次取数

```bash
python3 research_data.py research --symbols 002241
python3 research_data.py research --news --hotspots
python3 research_data.py research --symbols 002241 002242 --lhb-date 20261110
```

不指定 symbols 使用配置关注列表；重复股票、指数和同一类型／名称的板块去重。个股三个周期各一次，指数与板块各一次，新闻一次，行业与概念榜各一次；龙虎榜完整日期列表一次，并按全部选中股票处理席位。SQLite 写入串行，沿用现有缓存、错误冷却及 force 语义。不承诺固定网络耗时；未暴露超时的上游接口仍可能阻塞。

默认输出 Agent 文字证据简报：资格状态 → 共享市场／板块 → 个股计算观察 → 有界事件候选 → 集中缺口。数值带单位、时间和证据编号；空值翻译成原因，不当作零；失效序列只作历史参考，不产生当前强弱观察。具体加工规则见 [Agent 证据加工](agent-evidence.md)。

需要同时保留原始状态时，可加 `--snapshot-out /本轮新建临时目录/snapshot.json`，同次调用保存机器快照而 stdout 仍按所选格式输出；目标必须是新文件且父目录已存在，绝不覆盖。记录该次 stdout、stderr 与退出码的方法见[手动流程](manual-research.md#3-保存临时原始输出只向模型展示摘要)，不属于正式报告保存。

机器接口保留 `--format json`；`--pretty` 隐含 JSON 并缩进（兼容旧用法）。下文的 meta、issues 等字段描述机器快照契约，不要求 Agent 逐字段解释。共享市场、板块和事件只展示一次；每股引用事件 ID，最多展示三条目标候选和两条宏观候选，并明确候选是否截断。完整事件／链接、长历史和更多转折仍可按需用 context、bars 查看，简报不是底层数据副本。

## 统一可用时间与历史回放

```bash
python3 research_data.py research --symbols 002241 --no-refresh
python3 research_data.py research --symbols 002241 --as-of 2026-11-11T10:50:20+08:00 --hotspots
python3 research_data.py context --symbol 002241 --as-of 2026-11-11T10:50:20+08:00
python3 research_data.py bars --symbol 002241 --timeframe 15 --as-of 2026-11-11T10:50:20+08:00
python3 research_data.py hotspots --type industry --as-of 2026-11-11T10:50:20+08:00
```

- `--as-of` 必须带时区，归一化为上海时间；拒绝未来或无时区时间。它是“当时已经采集可用”的上限，不是单纯 K 线结束时间。例如 10:49 才首次取得的 10:45 K 线，不会出现在 10:45 的历史回放。
- research 默认先采集，结束后固定 `meta.as_of`／`information_cutoff`，再以一个 SQLite 读视图读取全部证据。`aligned_15_bar_end` 是股板指共同结束时点，失配则为 null 并有错误；各周期保留真实 bar_end、source、captured_at，不能说上游报价同步。
- `--as-of` 或 `--no-refresh` 不请求数据源，不能与 `--force`、`--news`、`--lhb-date` 联用；仍可读取已有新闻、龙虎榜，`--hotspots` 在此模式只读取已有榜单。
- `acquisition` 是**本次调用**的采集记录：汇总更新／缓存／失败／冷却／待取得，并在 `results` 保留每个依赖的 key、status、错误和可用时间字段；不复制采集器大载荷。只读或历史回放为零项，不证明原采集成功，也不重建原批次；审计首次采集必须读其同次留存快照。
- 简报按 `meta.session` 区分交易研究与非交易结构评价；时段来自常规时刻表，仍受日历未核验限制。
- `generated_at` 是本次输出时间，与行情时间分开。采集／总耗时可供观察，但不是受控性能基准。
- 历史修订、新闻、榜单、龙虎榜、人工口径记录均受可用时间约束。新采集尝试追加到 `refresh_attempts`，避免之后成功覆盖过去失败；升级前已被覆盖的错误无法重建。历史缺少错误记录不等于证明当时全项成功。

## 集中状态与退出码

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| ready | 检查未发现问题；不代表预测已验证 | 0 |
| limited | 核心行情可读取，但有价格口径未核验、板块缺失、广度报价时间未核验或日历限制等 | 0 |
| incomplete | 指定采集失败／冷却／pending，核心序列缺失或不足、落后预期边界、重复结束时间或股板指失配等 | 1 |

当前无完整日历，因此正常结果通常也是 limited；不要将退出 0 当成全项通过。`issues` 带 scope、severity、code，失败股票仍保留在 stocks 内供局部研究，不能冒充整轮成功。不可用于该截面推断的序列标记 `reference_only=true`，描述标签为 unavailable；重复结束时间不任意选价，摘要 close 置空，相关日内与相对收益不输出。新闻／榜单等显式请求失败也不会被外层“任务已结束”掩盖。

未核验海外接口不会被调用或伪造点位；overseas 明示 not_collected。需要日韩时由会话另作有界核验。小时线未覆盖最新 15 分钟变化时，`hourly_covers_latest_15m=false`，不是自动判定周期反向。

## 价格口径：人工登记，不是自动核验

新增 `basis-check` 仅登记操作者已核验的日期覆盖、来源和其中所有除权除息日；命令不抓取或验证链接，不自动修改价格，也不更新板块映射。

**仅实际核验后才可执行。**单个除息公告只证明具体事件，不足以证明任意长区间没有其他公司行为。可把已知事件仅登记在事件当天，区间外继续未核验：

```bash
python3 research_data.py basis-check --symbol 002241 \
  --start 2026-11-04 --end 2026-11-04 --ex-date 2026-11-04 \
  --source-url 'https://epaper.cs.com.cn/zgzqb/html/2026-10/27/nw.D110000zgzqb_20261027_2-B040.htm' --confirm
```

若已完整核对一段区间，start/end 填实际覆盖范围，重复 `--ex-date` 列出其中所有有效日期；无除息日时不填该项。禁止为了恢复指标而扩大未核验覆盖。end 不得晚于登记日；登记时间由程序实际写入，不允许 CLI 回填过去核验时间。

记录追加保存于 `price_basis_checks`。同一股票、相同覆盖区间的更正使用最新可用版本；不同覆盖中仍有已知冲突时保守停用，需重新核验相应区间，不能用更宽的“无事件”登记抹掉已有事件。

状态分别为 `same_session`（同日价格比较）、`verified`（人工覆盖内且没有跨事件）、`crosses_ex_date`、`unverified`。前两者允许相应比较；后两者逐指标置空：

- N 根收益检查 N+1 根价格；均线值检查 N 根，斜率检查 N+1 根，因此可出现“均线值保留、斜率为空”。
- 区间、振幅、参考转折、同时间成交额比值检查各自实际依赖窗口，不能继续用被屏蔽的结果。
- 日内收益需要已完成的前一会话收盘、当时已可用，且跨日口径合格；无资格时返回 null 和 reason。不以盘中最后一条记录假称昨收。
- 原始 bars 不变；context 和 research 的指标已应用检查。描述标签缺少必要指标时为 insufficient，不自动选震荡，更不是最终方向。
