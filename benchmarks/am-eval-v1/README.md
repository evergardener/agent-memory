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
第四轮隔离模型 runner 的 readiness 证据见
[`round-4-atomic-runner-readiness.json`](round-4-atomic-runner-readiness.json)，可复现操作见
[`../../docs/V1.0-AM-Eval隔离模型评测运行手册.md`](../../docs/V1.0-AM-Eval隔离模型评测运行手册.md)。
成功/超时路径、调用预算和非零退出码 Gate 见
[`round-4-atomic-runner-failure-gate.json`](round-4-atomic-runner-failure-gate.json) 及
[`../../docs/V1.0-AM-Eval第四轮Runner故障门禁报告.md`](../../docs/V1.0-AM-Eval第四轮Runner故障门禁报告.md)。
完整 CLI、真实 LiteLLM 和回环 OpenAI 兼容端点 Gate 见
[`round-4-atomic-runner-cli-gate.json`](round-4-atomic-runner-cli-gate.json) 及
[`../../docs/V1.0-AM-Eval第四轮CLI端到端门禁报告.md`](../../docs/V1.0-AM-Eval第四轮CLI端到端门禁报告.md)。
私有生产派生 gold 的仓库外初始化、复核、隐私扫描与冻结规则见
[`../../docs/V1.0-AM-Eval私有盲测金标工作流.md`](../../docs/V1.0-AM-Eval私有盲测金标工作流.md)。
工具链首轮完整验证证据见 [`round-5-private-gold-tooling-readiness.json`](round-5-private-gold-tooling-readiness.json)
及 [`../../docs/V1.0-AM-Eval第五轮私有金标工具验证报告.md`](../../docs/V1.0-AM-Eval第五轮私有金标工具验证报告.md)。
冻结生命周期操作契约及运行方法见
[`../../docs/V1.0-AM-Eval生命周期Gate运行手册.md`](../../docs/V1.0-AM-Eval生命周期Gate运行手册.md)。

验证冻结数据集：

```bash
uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json

uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/atomic-quality-selftest-v1/manifest.json

uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/lifecycle-gold-v1/manifest.json
```

M01/M02/M03/M07 使用 `agent-memory-score-atomic-quality <manifest> <output>`；M22/M23 使用
`agent-memory-score-efficiency <aggregate-input>`。合成 oracle 只验证评分器算术，不能写入正式 run。
真实外部模型运行必须先用 `agent-memory-plan-atomic-benchmark` 生成 metadata-only allowlist，再由
`agent-memory-run-atomic-benchmark` 在全新 `am_eval_` 隔离数据库执行；合成与生产派生数据使用不同
确认口令。
20 组生命周期操作使用 `agent-memory-run-lifecycle-benchmark`，只允许 loopback 上全新的
`am_eval_` 空数据库和仓库外受限输出；公开合成结果不计为 blind benchmark。详细边界见运行手册。

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
