# A股本地研究

保留原有 market_monitor 命令。新增单机 Python + SQLite + Claude CLI 研究流程，不提供定时器和自动下单。

## 主要模式：手动使用 Claude Agent

在项目根目录手动启动已安装并登录的 `claude`，向它提出研究任务，例如“分析 002241 的 15—60 分钟走势”。Claude 按 `CLAUDE.md` 和 `docs/claude/manual-research.md` 自主选择、执行数据脚本并解释结果。脚本调用可能需要你按 Claude CLI 的权限提示授权；网络不可用、数据不足时须报告限制。项目不会替你启动 Claude。

首次安装依赖并运行 `python stock_agent.py init`；按需配置 `config.json` 的板块映射。独立数据接口可用 `python research_data.py --help` 查看。手动研究结果不会自动写入 `analysis_runs` 或进入 `evaluate`，除非另行实现保存机制。

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

`python research_data.py refresh --symbol 002241 --news --hotspots` 按缓存状态逐项采集，`--lhb-date YYYYMMDD` 可额外采集指定日龙虎榜；`--force` 忽略缓存；`config.json` 的 `refresh_ttl_seconds` 可设置缓存最长秒数。默认只抓个股、配置的指数及关联板块，新闻、龙虎榜和板块榜单均需显式指定。各项成功、失败和上次成功时间保存在 SQLite；存在失败时返回非零，部分成功数据仍保留。缓存年龄和时间边界仅用于减少请求，不保证数据源新鲜或已开市。

`python research_data.py hotspots --type industry --limit 10` 读取最近的板块涨幅榜及上次排名，不采集全市场个股。榜单依赖 AKShare 的东方财富板块列表，覆盖、更新频率及实时性需现场验证；目前无成交额排名。未采集榜单时返回 missing；候选板块需要进一步核查。当前未接入完整交易日历、实时快照或可控制底层请求的超时/重试策略；工作日不能保证开市。

健壮性说明：单项采集失败时，未提交的数据库事务会回滚，已成功的数据及其采集时间会保留；上次失败的项目下次不会因旧缓存而被跳过。采集器如果已经分批提交，之前成功的批次不会回滚，需以逐项失败状态判断完整性。逐项检查缓存时使用当前时刻，避免长时间采集跨越 K 线边界。AKShare 部分接口未暴露统一超时设置，异常网络仍可能使调用长时间阻塞；建议在外层设置进程超时并检查退出状态，超时后不要把中断任务当作成功。尚未接入完整交易日历，也未完成真实 Claude 会话验证。
