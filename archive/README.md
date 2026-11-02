# 历史行情数据库归档

`market_data-before-untracking.sqlite3` 是提交 `2678ac1` 时原仓库跟踪的 `market_data.sqlite3` 的**冻结副本**，用于升级后的数据恢复；它不是持续更新的行情数据库。

自本次修复起，根目录 `market_data.sqlite3` 不再受 Git 跟踪。**拉取包含删除旧数据库的提交前，应先备份本机运行中的数据库**，以免丢失归档后新增的行情：

```sh
python3 -c "import sqlite3; source=sqlite3.connect('market_data.sqlite3'); target=sqlite3.connect('market_data.backup.sqlite3'); source.backup(target); target.close(); source.close()"
```

升级后，如果运行数据库不存在且没有更新的本地备份，可从冻结副本恢复：

```sh
cp -n archive/market_data-before-untracking.sqlite3 market_data.sqlite3
```

请勿用冻结副本覆盖具有更新行情的运行数据库；升级后仍需使用 SQLite backup API 定期备份。Git 历史保留原始提交，不进行历史改写。
