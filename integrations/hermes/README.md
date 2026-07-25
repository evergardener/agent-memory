# Hermes MemoryProvider 集成

`agent_memory/` 是正式 V1 Provider 开发目录。它通过本地 HTTP API 接入，不访问数据库，也不保存 Vault 明文。

当前已支持：

- Hermes 原生 `MemoryProvider` 发现与初始化；
- `on_turn_start`、`prefetch`、`sync_turn`、session switch；
- 用户/助手消息、工具调用参数和工具结果的事件提交；
- 多 profile 共享 namespace 并保留来源；
- 显式 `agent_memory_recall` 工具；
- 不依赖语义匹配的最近记忆浏览 `agent_memory_browse`；
- 来源追溯 `agent_memory_trace_source` 与用户更正 `agent_memory_correct` 工具；
- 仅在显式、未过期、profile 匹配的 grant 下使用 `agent_memory_use_protected_resource`；
- API 不可用时不阻断 Hermes 主回合。

已支持 `agent_memory_current_state`、`agent_memory_update_current_state` 与压缩前连续性摘要。

Hermes 在 `sync_turn(..., messages=...)` 中提供截至当前回合的完整消息列表。Provider
只读取最后一个用户消息之后的工具调用和工具结果；用户与助手正文使用独立参数提交。
不得重新遍历前序回合，否则会把累计工具历史重复写入新的 turn。

显式召回只表示“查询是否命中”，不能作为写入确认。`agent_memory_browse` 默认限定当前
profile，支持 `source_profile`、`fact_type`、`state`、`updated_after` 和 `limit`；
返回脱敏事实、状态、更新时间和 evidence IDs，可继续交给
`agent_memory_trace_source` 追溯。

## 安装与卸载

先启动 Agent Memory，再按实际加载范围安装。对于 `jiuyue` 这类 Hermes profile，
插件必须安装到 profile 自己的插件目录：

```bash
python3 scripts/hermes-plugin.py install \
  --hermes-home "${HERMES_HOME:-$HOME/.hermes}" \
  --profile jiuyue
hermes -p jiuyue memory setup agent_memory
```

`memory setup` 只在首次配置 Provider 时执行。日常升级使用：

```bash
python3 scripts/hermes-plugin.py upgrade \
  --hermes-home "${HERMES_HOME:-$HOME/.hermes}" \
  --profile jiuyue
```

升级是文件替换，不会热更新已运行的 Agent。完成后退出旧 Agent 进程并新开
`jiuyue` session；启动日志应显示 Provider 已激活，新版应包含
`agent_memory_browse`。不要只在旧 session 中重复查询来判断升级是否生效。

不传 `--profile` 时才安装到 `$HERMES_HOME/plugins/agent_memory` 全局目录。全局目录和
profile 目录若同时存在，以 Hermes 实际加载日志为准；不要把全局安装成功误认为指定
profile 已更新。

脚本只会升级带 `.agent-memory-managed` 标记的目录；遇到同名非托管目录会拒绝覆盖。卸载同样只删除该托管目录，不自动改写 Hermes 其他配置：

```bash
python3 scripts/hermes-plugin.py uninstall \
  --hermes-home "${HERMES_HOME:-$HOME/.hermes}" \
  --profile jiuyue
```

升级后按以下顺序验收：

1. `agent_memory_current_state`：确认 API 可用且 namespace/profile 正确；
2. 写入一条不含敏感信息、可明确识别的长期偏好；
3. `agent_memory_browse`：先确认最近写入已经投影，不依赖语义匹配；
4. `agent_memory_recall`：再以同义主题查询，确认语义召回；
5. `agent_memory_trace_source`：抽查 evidence 可追溯。

空 `recall` 只表示无召回资格的匹配，不能单独证明写入失败；先用 `browse` 区分
“没有写入/尚未投影”和“已写入但语义未命中”。

## 真实 Hermes 源码测试

```bash
PYTHONPATH="$HERMES_AGENT_ROOT:$PWD" \
AGENT_MEMORY_SERVICE_TOKEN='<本地服务令牌>' \
AGENT_MEMORY_API_URL='http://127.0.0.1:7788' \
"$HERMES_AGENT_ROOT/venv/bin/python" -m unittest \
  integrations.hermes.tests.test_live_provider -v
```

测试使用临时 `HERMES_HOME` 做插件发现，不读取或修改现有 profile 配置。
