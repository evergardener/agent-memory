# AM-Eval v1

本目录保存机器可读评估规范和不可覆盖的轮次测量。语义、数据集、硬门禁和执行流程见
[`../../docs/V1.0-AM-Eval长期记忆评估规范.md`](../../docs/V1.0-AM-Eval长期记忆评估规范.md)。
第一轮结论和缺口见
[`../../docs/V1.0-AM-Eval第一轮基线评估报告.md`](../../docs/V1.0-AM-Eval第一轮基线评估报告.md)。
第二轮隔离运行、恢复验证和剩余缺口见
[`../../docs/V1.0-AM-Eval第二轮隔离基线评估报告.md`](../../docs/V1.0-AM-Eval第二轮隔离基线评估报告.md)。
第三轮质量测量工具、合成自检和真实运行前置条件见
[`../../docs/V1.0-AM-Eval第三轮质量评测基础设施报告.md`](../../docs/V1.0-AM-Eval第三轮质量评测基础设施报告.md)。
机器可读 readiness 证据见 [`round-3-measurement-readiness.json`](round-3-measurement-readiness.json)。

验证冻结数据集：

```bash
uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json

uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/atomic-quality-selftest-v1/manifest.json
```

M01/M02/M03/M07 使用 `agent-memory-score-atomic-quality <manifest> <output>`；M22/M23 使用
`agent-memory-score-efficiency <aggregate-input>`。合成 oracle 只验证评分器算术，不能写入正式 run。

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
- 真实/生产派生 blind 数据只能保存在私有、受限目录；公开仓库只提交 manifest 摘要和聚合结果。
