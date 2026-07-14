# Hermes Memory Provider 集成 Spike

这是一个隔离的、内存型验证原型，不是 V1 的生产实现。

它验证四件事：

1. Hermes `MemoryProvider` 生命周期能够接收会话、回合、工具结果和结束事件。
2. 不同 Hermes profile 可通过显式 `shared_namespace` 进入同一 agent-memory 命名空间。
3. Provider 能在下一回合返回带来源的召回上下文，并暴露显式回忆/来源追溯工具。
4. Provider 可以作为用户安装的 Memory Provider 被 Hermes 发现，而不写入现有 `~/.hermes`。

## 运行

```bash
export HERMES_AGENT_ROOT=/Users/evergarden/.hermes/hermes-agent
PYTHONPATH="$HERMES_AGENT_ROOT:$PWD" "$HERMES_AGENT_ROOT/venv/bin/python" -m unittest \
  spikes.hermes_memory_provider_spike.test_spike -v
```

测试仅在临时目录创建一个独立 `HERMES_HOME`，不会读取或修改现有 Hermes profile、会话、凭据或生产记忆。

## 已验证结果

- `MemoryManager` 会调用 provider 的初始化、回合开始、异步回合同步、召回和会话切换接口。
- 完成回合时可接收 OpenAI 风格的工具结果消息，作为工具证据的输入。
- 两个 Hermes profile 可保留各自来源标记，同时通过显式 `shared_namespace` 共享同一记忆空间。
- Hermes 能从隔离 `HERMES_HOME/plugins/agent_memory_probe/` 发现该用户安装的 Provider。
