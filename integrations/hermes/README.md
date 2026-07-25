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

先启动 Agent Memory，再安装到用户插件目录：

```bash
python3 scripts/hermes-plugin.py install --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
hermes memory setup agent_memory
```

脚本只会升级带 `.agent-memory-managed` 标记的目录；遇到同名非托管目录会拒绝覆盖。卸载同样只删除该托管目录，不自动改写 Hermes 其他配置：

```bash
python3 scripts/hermes-plugin.py uninstall --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
```

## 真实 Hermes 源码测试

```bash
PYTHONPATH="$HERMES_AGENT_ROOT:$PWD" \
AGENT_MEMORY_SERVICE_TOKEN='<本地服务令牌>' \
AGENT_MEMORY_API_URL='http://127.0.0.1:7788' \
"$HERMES_AGENT_ROOT/venv/bin/python" -m unittest \
  integrations.hermes.tests.test_live_provider -v
```

测试使用临时 `HERMES_HOME` 做插件发现，不读取或修改现有 profile 配置。
