# A股本地研究

保留原有 market_monitor 命令。新增单机 Python + SQLite + Claude CLI 研究流程，不提供定时器和自动下单。

## 主要模式：手动使用 Claude Agent

在项目根目录手动启动已安装并登录的 `claude`，向它提出研究任务，例如“分析 002241 的 15—60 分钟走势”。Claude 按 `CLAUDE.md` 和 `docs/claude/manual-research.md` 自主选择、执行数据脚本并解释结果。脚本调用可能需要你按 Claude CLI 的权限提示授权；网络不可用、数据不足时须报告限制。项目不会替你启动 Claude。

首次安装依赖并运行 `python stock_agent.py init`；按需配置 `config.json` 的板块映射。`stock_names` 为个股新闻标题筛选提供名称，不能替代人工核对的板块映射。独立数据接口可用 `python research_data.py --help` 查看。手动研究结果不会自动写入 `analysis_runs` 或进入 `evaluate`，除非另行实现保存机制。

### 多周期研究与效果闭环

新增 [多周期目标、执行与验证规范](docs/claude/multi-horizon.md)：10–20分钟、2–3小时、今日收盘，以及下一/第二交易日分别判断。`python3 forecast.py --help` 提供 prepare → 人工综合 → record → evaluate，以及 baseline / score / compare / walk-forward / calibrate。没有自动调用模型、交易或调度。

`prepare` 支持显式股票/板块、策略版本、Git版本断言、可追溯日历/价格口径资料与同次快照；只有合格档才能给方向。退出0为有可分析档、1为采集/运行错误、2为全部档被阻断。默认阈值未经校准；真实数据源、核验覆盖和样本外效果仍须验收，不能将测试通过当作预测有效。

### 快速判定报告

非交易时段仅给收盘／最近交易时段结构评价，不外推下一交易时段方向；跨休市重新核对新证据。默认报告按 [快速报告模式](docs/claude/report-format.md) 输出：**结论摘要 → 市场简表 → 股票判定表 → 必要增量与限制**。每股一个主要判定、关键依据、较上轮变化和两个最重要的观察条件；缠论、波浪等只作内部框架，不逐项展开。指定单股不扩展为七股扫描；未指定股票的完整分析覆盖配置范围；“更新”只报告新变化。

取数优先使用 [批量研究入口](docs/claude/research-command.md)：

```bash
python3 research_data.py research --symbols 002241              # 单股、关联板块及指数
python3 research_data.py research --news --hotspots              # 配置关注列表，共享数据去重
python3 research_data.py research --symbols 002241 --no-refresh  # 只读当前已存数据，不联网
python3 research_data.py research --symbols 002241 --as-of 2026-11-11T10:50:20+08:00
```

默认输出经过资格检查的 Agent 文字证据简报（带单位、时间、证据编号和缺失原因），不再要求模型解读嵌套 JSON。需要同次留存原采集状态时使用 `--snapshot-out <新文件路径>`（不覆盖），无需为另一格式重复研究；机器快照 `acquisition.results` 保留逐项状态，回放零计数不是原采集证明。程序接入和诊断使用 `--format json`，`--pretty` 继续输出缩进 JSON。显式 Python 编排也使用同一加工层，模型的决策返回仍为 JSON。详见 [证据加工逻辑](docs/claude/agent-evidence.md)。

统一的是数据可用截止时间与 SQLite 读视图，不保证上游报价同步。摘要集中给出失败、缺失、过期和价格口径风险；`incomplete` 退出 1，`limited` 退出 0 但不代表全部验证通过。`context / bars / hotspots` 也支持 `--as-of`。价格口径依赖显式人工登记，非自动公告核验；没有核验覆盖的跨日指标置空，不默认通过。目标是更快交付有效结论，不承诺固定耗时或已验证的预测收益。

## 保留模式：Python 编排

```bash
python -m pip install -r requirements.txt
python stock_agent.py init
python stock_agent.py news
python stock_agent.py run --symbol 002241
python stock_agent.py show --symbol 002241
python stock_agent.py evaluate
python -m unittest discover -s tests -v
```

`init` 会在现有数据库旁创建 `.bak` 后增量建表。`run` 不指定 symbol 时遍历关注列表；需本机安装并配置 Claude CLI。`config.json` 的 sector_mapping 需人工核对后配置，例如 `"002241": {"industry": "行业名", "concept": ["概念名"]}`；没有映射时板块数据为空。news 使用 Google News RSS 发现线索，报道未核实，不等于事实；可调整搜索词及数据源。网络、数据接口或 Claude 失败时命令返回非零状态。

K线为不复权，时区为上海，采集时间与历史修订分开存储；以东方财富的分钟终止时间为假设，非交易日尚无权威日历，收盘集合竞价纳入最后一根。跨除权日不能直接解释结构。个股成交量按手转换为股；板块成交量单位不确定而留空。指标是描述性研究，不是经过校准的预测。持仓和可成交性未知，决策被限制为 WAIT；结果评价仅衡量方向，不等同于交易收益。免费数据源结构与可用性可能变化，首次使用请对照行情源核验时间和单位。

## 龙虎榜与事件聚合

```bash
python stock_agent.py lhb --date 20261110                 # 关注列表，先 AKShare，失败后直连网页数据接口
python stock_agent.py lhb --date 20261110 --source web     # 直连东方财富数据中心网页接口
python stock_agent.py lhb --date 20261110 --all --no-seats
python stock_agent.py aggregate                           # 对未归组报道增量归组
```

两条龙虎榜通路均依赖东方财富，并非独立交叉核验。上榜记录按股票、交易日、披露原因和采集时间保存；席位按买卖方向、营业部和披露原因保存。上榜列表为空可能是尚未披露或接口异常，命令返回失败，不能理解为确认没有上榜。席位抓取失败时保留成功的上榜数据并返回非零状态。龙虎榜为盘后披露，分析仅使用当时已采集数据，不代表全市场资金或游资身份。

新闻按来源 URL 保存，归组仅在同分类、48 小时内、标题高度相似且数字一致时进行；来源、发布时间、首次采集及归组算法版本均保留。相似标题是保守的候选匹配，不保证同一事件，也无法将措辞差异较大的同一事件全部合并。事件状态始终为未核实；不会自动生成事实、影响或置信度。历史上下文只包含当时已采集并已归组的报道，避免未来信息泄漏。

可通过 `python stock_agent.py news --extract` 在采集归组后调用 Claude 批量提取，或用 `python stock_agent.py enrich` 单独处理尚未分析或新增了报道的事件。提取结果包括带文章 ID 的报道主张、影响对象与机制、方向、验证与证伪条件；这些是模型提取的未核实内容。程序检查结构和引用归属，并按实际提取完成时间追加保存，历史查询不会看到未来提取结果。此功能需要本机 Claude CLI 已安装并完成认证。

Claude 的任务入口及按需参考文档见 `CLAUDE.md`。自动分析和事件提取会分别注入对应的参考规范；这两个非交互 Claude 子进程的工具访问仍然关闭。

## 统一按需采集

`python research_data.py refresh --symbol 002241 --news --hotspots` 按 [数据更新生命周期](docs/claude/update-policy.md) 逐项采集，`--lhb-date YYYYMMDD` 可额外采集指定日龙虎榜；`--force` 跳过复用和错误冷却；`config.json` 的 `refresh_ttl_seconds` 与 `update_policy` 分别配置可变数据间隔和生命周期规则。日线及分钟线按已闭合边界更新；历史龙虎榜按日期持久复用，不因 24 小时到期重抓；成分名单和广度快照分开管理。默认只抓个股、配置的指数及关联板块，新闻、龙虎榜和板块榜单均需显式指定。各项成功、失败和上次成功时间保存在 SQLite；存在失败或失败冷却时返回非零，部分成功数据仍保留；`pending` 表示尚未取得目标数据，不能当作完整成功。单项 `collect`、`members`、独立 `news`、`lhb` 及保留的 `run` 共用更新机制。缓存年龄和时间边界仅用于减少请求，不保证数据源新鲜或已开市。

`python research_data.py hotspots --type industry --limit 10` 读取最近的板块涨幅榜及上次排名，不采集全市场个股。榜单优先依赖 AKShare 的东方财富板块列表；动态涨跌幅排序分页出现同代码冲突时，回退到同源网页接口按板块代码稳定分页，并校验总量及重复代码。这不是独立交叉核验，覆盖、更新频率及实时性仍需现场验证；目前无成交额排名。未采集榜单时返回 `missing`；快照涨跌幅没有横截面差异（例如全零）时返回 `unusable`，不提供有效名次。并列涨跌幅共享名次；只有相邻两次快照均有效且板块代码集合一致时才比较排名变化。完全相同的分页重复项去重并记录数量；同一代码的数据冲突若在稳定分页回退后仍存在，整次榜单采集失败，不静默覆盖。最近榜单采集失败且存在旧快照时，`hotspots` 返回 `stale` 和 `last_error`，不输出有效名次或排名变化。候选板块需要进一步核查。当前未接入完整交易日历、实时快照或可控制底层请求的超时/重试策略；工作日不能保证开市。

健壮性说明：单项采集失败时，未提交的数据库事务会回滚，已成功的数据及其采集时间会保留；上次失败不会因旧缓存被洗成成功，默认 300 秒冷却期内返回 `deferred` 和原错误，期满后由下次显式调用重试。采集器如果已经分批提交，之前成功的批次不会回滚，需以逐项失败状态判断完整性。逐项检查缓存时使用当前时刻，避免长时间采集跨越 K 线边界。AKShare 部分接口未暴露统一超时设置，异常网络仍可能使调用长时间阻塞；建议在外层设置进程超时并检查退出状态，超时后不要把中断任务当作成功。尚未接入完整交易日历，也未完成真实 Claude 会话验证。
