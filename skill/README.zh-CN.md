# Hermes Personal Memo

[English](README.md) | [简体中文](README.zh-CN.md)

面向 [Hermes](https://hermes-agent.nousresearch.com/) 的本地优先备忘录、待办、链接与提醒管理工具。Personal Memo 使用 SQLite 保存数据，并通过 Hermes 原生插件、命令行适配器和可选 MCP Server 共享同一套业务逻辑。

## 功能特点

- Hermes 原生工具：新增、查询、搜索、修改、完成、归档、恢复与删除事项。
- 本地 SQLite 数据库：WAL 模式、事务、幂等写入、按会话编号视图、备份与完整性检查。
- 支持任务、笔记、链接、文章和视频，并内置 URL 规范化与重复处理。
- 支持精确提醒、仅日期截止时间、计划日期、延期日期、操作历史和 Markdown 导出。
- 默认时区为 `Asia/Shanghai`；迁移已有事项时只改变显示时区，不会改变精确时间所对应的绝对时刻。
- 可选 stdio MCP Server，供 Hermes 以外的 MCP 客户端访问同一份数据。
- 支持当前数据库结构，以及最早期未版本化 Personal Memo 数据库的安全迁移。

## 架构

```text
Hermes 原生插件 ─┐
                ├─> personal_memo_core ─> SQLite 数据库
可选 MCP Server ─┘
                └─> 备份与迁移
```

`personal_memo_core` 集中管理数据库、迁移、备份、并发控制和业务逻辑。Hermes 插件与 MCP Server 都只是适配层，不会维护第二套业务逻辑或数据库。

## 环境要求

- Python 3.10 或更高版本。
- 使用 Hermes 原生插件时，需要已安装并可运行 Hermes。
- Hermes 插件和命令行不需要第三方 Python 依赖。
- MCP 为可选功能，依赖写在 `memo-mcp/requirements-mcp.txt` 中。

## 安装或升级

下载并解压发布包后执行：

```bash
cd personal-memo-hermes-layered
python3 install.py
hermes plugins enable personal-memo
```

启用后重启 Hermes gateway，或新开一个 Hermes 会话。

安装器会：

1. 替换已有的核心库、Skill、插件和 MCP Server 文件；组件代码不创建旧版本备份。
2. 将共享核心库安装到 `~/.hermes/lib/personal_memo_core`。
3. 安装 Hermes Skill 与原生插件。
4. 将可选 MCP Server 安装到 `~/.hermes/mcp-servers/personal-memo`。
5. 在需要时先创建数据库备份，再迁移已有 Personal Memo 数据。
6. 将备忘录默认时区和所有事项的显示时区设为 `Asia/Shanghai`，但不改写已保存的精确时间。

## 在 Hermes 中使用

直接用自然语言与 Hermes 对话：

```text
备忘一下：下周五下午三点回复客户。
明天上午十点提醒我交房租。
保存 https://example.com，之后阅读。
今天有什么待办？
搜索之前保存的 Python 链接。
把第 2 条完成。
把项目会议改到下周一下午。
```

原生插件会注册 `memo_add`、`memo_list`、`memo_show`、`memo_search`、`memo_today`、`memo_update` 和 `memo_transition` 等工具。通常无需手动执行 Python 命令，Hermes 会根据自然语言请求选择工具。

如需由插件直接返回易读列表，请使用 `/memos`。使用 `/memos all` 可显示已完成、已删除和已归档事项。

手动确认时区或数据库状态：

```bash
HERMES_HOME="$HOME/.hermes" \
python3 "$HOME/.hermes/skills/productivity/personal-memo/scripts/memo.py" \
migrate-timezone Asia/Shanghai --json
```

## 可选：MCP Server

只有在希望让 IDE、桌面客户端等其他 MCP 客户端访问同一份备忘录时，才需要配置 MCP。仅使用 Hermes 原生插件时无需额外配置。

安装 MCP 依赖：

```bash
python3 -m pip install -r ~/.hermes/mcp-servers/personal-memo/requirements-mcp.txt
```

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "personal-memo": {
      "command": "python3",
      "args": ["/home/YOUR_USER/.hermes/mcp-servers/personal-memo/server.py"],
      "env": {
        "HERMES_HOME": "/home/YOUR_USER/.hermes"
      }
    }
  }
}
```

MCP Server 使用 stdio 通信，提供 `memo_add`、`memo_list`、`memo_show`、`memo_search`、`memo_today`、`memo_update`、`memo_complete`、`memo_delete`、`memo_backup` 和 `memo_timezone_migrate` 等工具。

## 项目结构

```text
personal_memo_core/  与框架无关的数据库、迁移、备份与业务逻辑
hermes-plugin/       Hermes Schema、注册和工具处理器
memo-mcp/            可选 stdio MCP 适配器
scripts/memo.py      保持兼容的命令行适配器
tests/               回归测试与 Hermes 插件测试
```

## 数据与安全

- 默认数据库路径：`~/.hermes/data/personal-memo/memos.sqlite3`。
- 使用 SQLite WAL、外键、忙等待和插件线程本地连接，保护并发访问。
- 写入会创建轮换的写前备份；可通过 CLI 或 MCP 工具手动创建备份。
- 默认删除为软删除；物理清除和恢复备份需要精确确认令牌。
- Hermes 插件和 MCP Server 必须使用相同的 `HERMES_HOME` 或 `PERSONAL_MEMO_DATA_DIR`，才能共享数据。

## 开发

在项目根目录运行完整测试：

```bash
python3 -m unittest discover -s tests -v
```

## 贡献

欢迎提交 Issue 和 Pull Request。请将业务行为保留在 `personal_memo_core` 中；Hermes 与 MCP 的改动应保持为围绕共享核心的轻量适配层。
