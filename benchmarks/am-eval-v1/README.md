# AM-Eval v1

本目录保存机器可读评估规范和不可覆盖的轮次测量。语义、数据集、硬门禁和执行流程见
[`../../docs/V1.0-AM-Eval长期记忆评估规范.md`](../../docs/V1.0-AM-Eval长期记忆评估规范.md)。
第一轮结论和缺口见
[`../../docs/V1.0-AM-Eval第一轮基线评估报告.md`](../../docs/V1.0-AM-Eval第一轮基线评估报告.md)。
第二轮隔离运行、恢复验证和剩余缺口见
[`../../docs/V1.0-AM-Eval第二轮隔离基线评估报告.md`](../../docs/V1.0-AM-Eval第二轮隔离基线评估报告.md)。

验证冻结数据集：

```bash
uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json
```

运行评分器：

```bash
uv run agent-memory-benchmark \
  benchmarks/am-eval-v1/spec.json \
  benchmarks/am-eval-v1/round-2-agent-memory.json
```

规则：

- 结果文件只能新增新 run，不覆盖旧 run；
- `not_measured` 不得手工改成 0 或 100%；
- evidence 只能引用本轮实际执行的测试、聚合报告或固定 SHA 数据集；
- 不在本目录保存生产消息正文、模型 prompt/response、凭据或 Vault 明文。
- 公开 development/validation 样本只用于回归，不得计为 blind benchmark。
