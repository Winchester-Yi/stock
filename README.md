# A股本地研究

保留原有 market_monitor 命令。新增单机 Python + SQLite + Claude CLI 研究流程，不提供定时器和自动下单。

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
