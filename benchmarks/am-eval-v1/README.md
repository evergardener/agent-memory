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
第六轮实际运行证据见 [`round-6-lifecycle-gate.json`](round-6-lifecycle-gate.json) 及
[`../../docs/V1.0-AM-Eval第六轮生命周期门禁报告.md`](../../docs/V1.0-AM-Eval第六轮生命周期门禁报告.md)。
第七轮评测密钥文件安全门禁见
[`round-7-model-key-gate-readiness.json`](round-7-model-key-gate-readiness.json) 及
[`../../docs/V1.0-AM-Eval第七轮模型密钥门禁报告.md`](../../docs/V1.0-AM-Eval第七轮模型密钥门禁报告.md)。
第八轮冻结 execution plan、零网络 preflight 和 plan SHA 证据链见
[`round-8-execution-plan-gate.json`](round-8-execution-plan-gate.json) 及
[`../../docs/V1.0-AM-Eval第八轮执行计划门禁报告.md`](../../docs/V1.0-AM-Eval第八轮执行计划门禁报告.md)。
第九轮将实际事实提取上限冻结到 execution plan v3，证据见
[`round-9-fact-limit-binding-gate.json`](round-9-fact-limit-binding-gate.json) 及
[`../../docs/V1.0-AM-Eval第九轮提取上限绑定门禁报告.md`](../../docs/V1.0-AM-Eval第九轮提取上限绑定门禁报告.md)。
第十轮将模型超时、时间窗口、可信工具和数据库迁移 head 冻结到 execution plan v4，证据见
[`round-10-runtime-policy-schema-gate.json`](round-10-runtime-policy-schema-gate.json) 及
[`../../docs/V1.0-AM-Eval第十轮运行策略与Schema绑定门禁报告.md`](../../docs/V1.0-AM-Eval第十轮运行策略与Schema绑定门禁报告.md)。
第十一轮将 Git checkout、实际执行 package、镜像 build identity、plan/output 和评分器绑定到同一个
运行源身份，证据见 [`round-11-runtime-identity-gate.json`](round-11-runtime-identity-gate.json) 及
[`../../docs/V1.0-AM-Eval第十一轮运行身份绑定门禁报告.md`](../../docs/V1.0-AM-Eval第十一轮运行身份绑定门禁报告.md)。
第十二轮将任务终态与实际模型调用账本、数据集/输出文件描述符快照、评分输入 SHA、Python/依赖文件
指纹、正式测量 attestation 和精确 OCI digest 绑定纳入 fail-closed Gate，证据见
[`round-12-measurement-provenance-gate.json`](round-12-measurement-provenance-gate.json) 及
[`../../docs/V1.0-AM-Eval第十二轮测量来源与执行完整性门禁报告.md`](../../docs/V1.0-AM-Eval第十二轮测量来源与执行完整性门禁报告.md)。
第十三轮把质量与效率 scorer 的原始结果字节、SHA、运行/环境身份和原始计数绑定到 sourced
attestation，证据见
[`round-13-sourced-score-attestation-gate.json`](round-13-sourced-score-attestation-gate.json) 及
[`../../docs/V1.0-AM-Eval第十三轮评分来源证明门禁报告.md`](../../docs/V1.0-AM-Eval第十三轮评分来源证明门禁报告.md)。
第十四轮把冻结生命周期 runner 的 G05、G06、M15、M16、M17 从实际 case/invariant 计数来源化；G02
因现有 case 不直接测量越权召回而明确保持缺口。证据见
[`round-14-lifecycle-sourced-attestation-gate.json`](round-14-lifecycle-sourced-attestation-gate.json) 及
[`../../docs/V1.0-AM-Eval第十四轮生命周期来源证明门禁报告.md`](../../docs/V1.0-AM-Eval第十四轮生命周期来源证明门禁报告.md)。
第十五轮使用冻结 recall suite、真实 loopback HTTP API 和 metadata-only query ledger，将 G02、M05、
M06、M08、M21 从返回排名、状态码与原始耗时来源化。证据见
[`round-15-recall-sourced-attestation-gate.json`](round-15-recall-sourced-attestation-gate.json) 及
[`../../docs/V1.0-AM-Eval第十五轮召回来源证明门禁报告.md`](../../docs/V1.0-AM-Eval第十五轮召回来源证明门禁报告.md)，
复现边界见 [`../../docs/V1.0-AM-Eval召回Gate运行手册.md`](../../docs/V1.0-AM-Eval召回Gate运行手册.md)。
第十六轮使用冻结合成脱敏 probe、真实 ingest 持久化路径和逐事实 trace ledger，将 G01、G03、G09 从
event/fact/document 扫描、evidence link 与 exact trace event 来源化。证据见
[`round-16-evidence-sourced-attestation-gate.json`](round-16-evidence-sourced-attestation-gate.json) 及
[`../../docs/V1.0-AM-Eval第十六轮证据完整性来源证明门禁报告.md`](../../docs/V1.0-AM-Eval第十六轮证据完整性来源证明门禁报告.md)，
复现边界见 [`../../docs/V1.0-AM-Eval证据完整性Gate运行手册.md`](../../docs/V1.0-AM-Eval证据完整性Gate运行手册.md)。

验证冻结数据集：

```bash
uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/deterministic-gold-v1/manifest.json

uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/atomic-quality-selftest-v1/manifest.json

uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/lifecycle-gold-v1/manifest.json

uv run agent-memory-validate-benchmark-dataset \
  benchmarks/am-eval-v1/datasets/evidence-integrity-gold-v1/manifest.json
```

M01/M02/M03/M07 使用
`agent-memory-score-atomic-quality <manifest> <output> --plan <plan> --confirm-plan-sha256 <sha> --confirm-source-sha256 <sha> --confirm-output-sha256 <sha>`；M22/M23 使用
`agent-memory-score-efficiency <aggregate-input> --manifest <manifest> --plan <plan> --confirm-plan-sha256 <sha> --confirm-source-sha256 <sha> --confirm-input-sha256 <sha>`。两个评分器都会完整校验 plan，并核对当前评分代码、Python/关键依赖实际安装文件、run/model/policy、数据治理字段与输入工件 SHA。合成 oracle 只验证评分器
算术，不能写入正式 run。真实外部模型运行必须先用 `agent-memory-plan-atomic-benchmark` 在仓库外生成
metadata-only execution plan，再以 `agent-memory-preflight-atomic-benchmark` 完成零网络预检，最后由
`agent-memory-run-atomic-benchmark` 在全新 `am_eval_` 隔离数据库执行；合成与生产派生数据使用不同
确认口令，runner、质量评分和效率评分必须确认同一个 plan SHA。
私有 blind 质量结果和隔离 M23 在进入 formal run 前还必须通过
`agent-memory-assemble-eval-attestation`：组装器会嵌入原 scorer result 字节并从原始计数复算指标，
旧 quality/efficiency attestation v1 不再被正式聚合器接受。isolated M22 不能作为正式 M22。
20 组生命周期操作使用 `agent-memory-run-lifecycle-benchmark`，只允许 loopback 上全新的
`am_eval_` 空数据库和仓库外受限输出；公开合成结果不计为 blind benchmark。详细边界见运行手册。
召回来源 Gate 使用 `agent-memory-run-recall-benchmark`，要求 loopback API、专用 `am_eval_` 数据库和
自动化 namespace；其结果可由 `agent-memory-assemble-eval-attestation recall` 复算 G02、M05、M06、
M08、M21，但本地 checkout 结果不能冒充正式 image-backed run。
证据完整性 Gate 使用 `agent-memory-run-evidence-benchmark`，只允许全新 loopback `am_eval_` 数据库；
`agent-memory-assemble-eval-attestation evidence` 只能从 metadata-only case ledger 复算 G01、G03、G09。

运行评分器：

```bash
SPEC_SHA="$(shasum -a 256 benchmarks/am-eval-v1/spec.json | awk '{print $1}')"
RUN_SHA="$(shasum -a 256 benchmarks/am-eval-v1/round-2-agent-memory.json | awk '{print $1}')"
uv run agent-memory-benchmark \
  benchmarks/am-eval-v1/spec.json \
  benchmarks/am-eval-v1/round-2-agent-memory.json \
  --confirm-spec-sha256 "$SPEC_SHA" \
  --confirm-run-sha256 "$RUN_SHA"
```

规则：

- 结果文件只能新增新 run，不覆盖旧 run；
- `not_measured` 不得手工改成 0 或 100%；
- evidence 只能引用本轮实际执行的测试、聚合报告或固定 SHA 数据集；
- 不在本目录保存生产消息正文、模型 prompt/response、凭据或 Vault 明文。
- 公开 development/validation 样本只用于回归，不得计为 blind benchmark。
- 真实/生产派生 blind 数据只能保存在私有、受限目录；公开仓库只提交 manifest 摘要和聚合结果。
- 完整正式 run 只能在 `image-build-metadata` 运行身份下评分，并必须由操作员再次确认
  `name@sha256:<manifest-digest>` 与 `linux/amd64|linux/arm64`；Git checkout 只用于诊断。
