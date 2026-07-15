# Hermes Personal Memo

**简体中文** | [English](README.md)

面向 [Hermes Agent](https://hermes-agent.nousresearch.com/) 的本地优先个人备忘录、待办、链接与提醒管理插件。

Personal Memo 使用 SQLite 持久化数据，并通过 Hermes 原生插件、斜杠命令、CLI 适配器和可选 stdio MCP Server 共享同一套业务核心。它同时支持纯文本笔记、待办、链接、文章、视频、截止时间、提醒、操作历史、数据库备份和 schema 迁移。

## 功能特点

- Agent 辅助捕获：从自然语言提取摘要、条目类型、日期、提醒时间和优先级。
- 链接处理：URL 规范化、重复检测、受限元数据解析、来源摘要和失败重试。
- Hermes 原生工具与便捷命令：`/memos`、`/memos_add`、`/memos_detail`、`/memos_done`、`/memos_delete`、`/memos_edit`、`/memos_search`、`/memos_today`、`/memos_fresh_all` 等。
- 支持 Telegram 等聊天界面的稳定编号视图。
- SQLite WAL、事务、幂等写入、作用域快照、不可变操作历史、轮换数据库备份和完整性检查。
- 可选 MCP 访问，使用同一份数据库和核心业务逻辑。

## 架构

```text
Hermes 插件 ─────┐
斜杠命令 ────────┼──> personal_memo_core ──> SQLite
CLI / MCP 适配器 ─┘              └──────────> 备份与迁移
```

`personal_memo_core` 负责 schema、迁移、解析、来源抓取、状态转换、备份和业务规则。插件、CLI 与 MCP Server 都是围绕核心库的轻量适配层。

## 环境要求

- Python 3.10 或更高版本；
- 使用原生插件时需要已安装并可运行 Hermes；
- 插件和 CLI 不需要第三方 Python 依赖；
- 可选 MCP 依赖位于 `mcp/requirements-mcp.txt`。

## 安装

在仓库根目录执行：

```bash
python3 install.py
hermes plugins enable personal-memo
```

然后重启 Hermes Gateway 或新建会话。安装器会直接替换已安装的代码、Skill、插件和 MCP 适配器，不再创建组件目录备份；数据库备份和迁移保护仍由核心库负责。

默认数据库路径：

```text
~/.hermes/data/personal-memo/memos.sqlite3
```

## 常用命令

```text
/memos
/memos_add 明天下午整理实验数据
/memos_detail 2
/memos_done 2
/memos_delete 2
/memos_edit 2 改到下周一
/memos_search Python
/memos_today
/memos_fresh 2
/memos_fresh_all
```

刷新命令会结合 Hermes 当前配置的人设和用户画像重新执行结构化 LLM 提取，同时保留备忘录原始内容。

## 可选 MCP Server

安装可选依赖：

```bash
python3 -m pip install -r mcp/requirements-mcp.txt
```

然后从兼容 MCP 的客户端通过 stdio 启动 `mcp/server.py`。将其指向相同的 `HERMES_HOME`，即可让多个客户端共享同一数据库。

## 开发

```bash
python3 -m unittest discover -s skill/tests -v
python3 -m py_compile install.py core/personal_memo_core/service.py plugin/*.py
```

## 仓库结构

```text
core/personal_memo_core/  共享数据库和业务逻辑
plugin/                   Hermes 插件注册与处理器
mcp/                      可选 stdio MCP 适配器
skill/                    Hermes Skill、参考文档和测试
install.py                安装与升级入口
```

欢迎提交 Issue 和 Pull Request。请将业务行为保留在 `personal_memo_core` 中，框架适配层保持轻量。
