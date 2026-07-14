import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  ConsolidationReport,
  GraphData,
  StateData,
  VaultEntry,
  VaultGrant
} from "./api";
import { StarMap } from "./StarMap";

const emptyGraph: GraphData = { nodes: [], edges: [] };

function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      await api.login(password);
      onLogin();
    } catch {
      setError("密码不正确");
    }
  }
  return (
    <main className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="mark">✦</div>
        <p className="eyebrow">LOCAL MEMORY SYSTEM</p>
        <h1>记忆星图</h1>
        <p className="muted">查看、追溯与治理 Hermes 的长期记忆</p>
        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          placeholder="本地管理密码"
          autoFocus
        />
        {error && <p className="error">{error}</p>}
        <button type="submit">进入星图</button>
      </form>
    </main>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [graph, setGraph] = useState<GraphData>(emptyGraph);
  const [vault, setVault] = useState<VaultEntry[]>([]);
  const [grants, setGrants] = useState<VaultGrant[]>([]);
  const [selected, setSelected] = useState<Record<string, string> | null>(null);
  const [trace, setTrace] = useState<Record<string, unknown> | null>(null);
  const [stateData, setStateData] = useState<StateData | null>(null);
  const [reports, setReports] = useState<ConsolidationReport[]>([]);
  const [tab, setTab] = useState<"map" | "state" | "reports" | "vault">("map");
  const [message, setMessage] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [graphData, vaultEntries, activeGrants, status, reportItems] = await Promise.all([
        api.graph(),
        api.vaultEntries(),
        api.vaultGrants(),
        api.state(),
        api.reports()
      ]);
      setGraph(graphData);
      setVault(vaultEntries);
      setGrants(activeGrants);
      setStateData(status);
      setReports(reportItems);
      setAuthenticated(true);
    } catch (error) {
      if ((error as Error & { status?: number }).status === 401) setAuthenticated(false);
      else setMessage(String(error));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const counts = useMemo(
    () => ({
      facts: graph.nodes.filter((node) => node.data.kind === "fact").length,
      entities: graph.nodes.filter((node) => node.data.kind === "entity").length,
      protected: graph.nodes.filter((node) => node.data.kind === "vault").length
    }),
    [graph]
  );

  async function loadTrace() {
    if (selected?.kind !== "fact") return;
    setTrace(await api.trace(selected.record_id));
  }

  async function changeState(action: "forget" | "isolate") {
    if (selected?.kind !== "fact") return;
    const reason = window.prompt(action === "forget" ? "忘记原因" : "删除关联原因");
    if (!reason) return;
    await api.changeState(selected.record_id, action, reason);
    setSelected(null);
    await refresh();
  }

  async function correct() {
    if (selected?.kind !== "fact") return;
    const statement = window.prompt("修正后的事实", selected.label);
    if (!statement || statement === selected.label) return;
    const reason = window.prompt("修正原因") || "User correction from star map";
    await api.correct(selected.record_id, statement, reason);
    setSelected(null);
    await refresh();
  }

  async function purge() {
    if (selected?.kind !== "fact") return;
    const confirmation = window.prompt(`永久清除不可恢复。请输入记忆 ID：\n${selected.record_id}`);
    if (confirmation !== selected.record_id) return;
    const reason = window.prompt("永久清除原因") || "User requested permanent purge";
    await api.purge(selected.record_id, reason);
    setSelected(null);
    await refresh();
  }

  if (authenticated === null) return <main className="loading">正在连接本地记忆库…</main>;
  if (!authenticated) return <Login onLogin={refresh} />;

  return (
    <main className="app-shell">
      <header>
        <div>
          <p className="eyebrow">AGENT MEMORY · HERMES</p>
          <h1>记忆星图</h1>
        </div>
        <nav>
          <button className={tab === "map" ? "active" : ""} onClick={() => setTab("map")}>
            星图
          </button>
          <button className={tab === "state" ? "active" : ""} onClick={() => setTab("state")}>
            当前状态
          </button>
          <button className={tab === "reports" ? "active" : ""} onClick={() => setTab("reports")}>
            整理报告
          </button>
          <button className={tab === "vault" ? "active" : ""} onClick={() => setTab("vault")}>
            受保护资源
          </button>
          <button
            onClick={async () => {
              await api.logout();
              setAuthenticated(false);
            }}
          >
            退出
          </button>
        </nav>
      </header>

      <section className="stats">
        <span><strong>{counts.facts}</strong> 记忆</span>
        <span><strong>{counts.entities}</strong> 实体</span>
        <span><strong>{counts.protected}</strong> 受保护资源</span>
      </section>

      {message && <div className="banner">{message}</div>}

      {tab === "map" ? (
        <section className="workspace">
          <StarMap graph={graph} onSelect={setSelected} />
          <aside className="detail-panel">
            {selected ? (
              <>
                <p className="eyebrow">{selected.kind}</p>
                <h2>{selected.label}</h2>
                {selected.hint && <p className="vault-hint">{selected.hint}</p>}
                <dl>
                  {selected.state && <><dt>状态</dt><dd>{selected.state}</dd></>}
                  {selected.source_profile && <><dt>来源</dt><dd>{selected.source_profile}</dd></>}
                </dl>
                {selected.kind === "fact" && (
                  <div className="actions">
                    <button onClick={loadTrace}>追溯证据</button>
                    <button onClick={correct}>修正</button>
                    <button onClick={() => changeState("forget")}>忘记</button>
                    <button className="danger" onClick={() => changeState("isolate")}>删除关联</button>
                    <button className="danger purge" onClick={purge}>永久清除</button>
                  </div>
                )}
                {trace && <pre>{JSON.stringify(trace, null, 2)}</pre>}
              </>
            ) : (
              <div className="empty-detail">
                <span>✦</span>
                <p>选择星体查看证据、版本与治理操作</p>
              </div>
            )}
          </aside>
        </section>
      ) : tab === "vault" ? (
        <VaultPanel
          entries={vault}
          grants={grants}
          linkedMemory={selected?.kind === "fact" ? selected : null}
          onChange={refresh}
        />
      ) : tab === "state" ? (
        <StatePanel data={stateData} onChange={refresh} />
      ) : (
        <ReportsPanel reports={reports} />
      )}
    </main>
  );
}

const AXIS_LABELS: Record<string, string> = {
  interaction_need: "互动需求",
  restraint: "表达克制",
  valence: "情感效价",
  arousal: "激活度",
  immersion: "任务沉浸"
};

function StatePanel({ data, onChange }: { data: StateData | null; onChange: () => Promise<void> }) {
  const [draft, setDraft] = useState(data?.config || null);
  useEffect(() => setDraft(data?.config || null), [data?.config]);

  async function saveConfig() {
    if (!draft) return;
    await api.configureState(draft);
    await onChange();
  }

  async function resetState() {
    if (!window.confirm("重置互动状态到当前初始参数？历史状态快照将被清除。")) return;
    await api.resetState();
    await onChange();
  }

  async function simulateState() {
    const content = window.prompt("输入用于无副作用模拟的消息", "紧急排障 project:atlas");
    if (!content) return;
    const result = await api.simulateState(content);
    window.alert(`${result.summary}\n${JSON.stringify(result.axes, null, 2)}`);
  }

  return (
    <section className="status-page">
      <div className="page-intro">
        <p className="eyebrow">DETERMINISTIC · NO AUTONOMOUS ACTIONS</p>
        <h2>当前状态</h2>
        <p>{data?.interaction?.summary || "尚未生成互动状态快照。"}</p>
      </div>
      {data?.interaction && (
        <div className="axis-grid">
          {Object.entries(data.interaction.axes).map(([key, value]) => (
            <article key={key}>
              <span>{data.config.axis_labels[key] || AXIS_LABELS[key] || key}</span><strong>{Math.round(value * 100)}%</strong>
              <i><b style={{ width: `${value * 100}%` }} /></i>
            </article>
          ))}
        </div>
      )}
      {draft && (
        <section className="state-governance">
          <div>
            <h3>状态参数治理</h3>
            <p>暂停只停止生成新的互动状态快照；当前事实与跨 profile 连续性仍照常维护。</p>
          </div>
          <label className="state-toggle">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
            />
            {draft.enabled ? "状态计算已启用" : "状态计算已暂停"}
          </label>
          <label>
            回归初始值周期（小时）
            <input
              type="number"
              min="1"
              max="720"
              value={draft.drift_hours}
              onChange={(event) => setDraft({ ...draft, drift_hours: Number(event.target.value) })}
            />
          </label>
          <div className="state-axis-settings">
            {Object.entries(draft.axes_initial).map(([key, value]) => (
              <div className="axis-setting" key={key}>
                <label className="axis-enable">
                  <input
                    type="checkbox"
                    checked={draft.axis_enabled[key]}
                    onChange={(event) => setDraft({
                      ...draft,
                      axis_enabled: { ...draft.axis_enabled, [key]: event.target.checked }
                    })}
                  />
                  启用
                </label>
                <input
                  aria-label={`${key} 名称`}
                  value={draft.axis_labels[key]}
                  maxLength={64}
                  onChange={(event) => setDraft({
                    ...draft,
                    axis_labels: { ...draft.axis_labels, [key]: event.target.value }
                  })}
                />
                <label>
                  初始值 · {Math.round(value * 100)}%
                  <input
                    type="range"
                    min={draft.axis_ranges[key].min}
                    max={draft.axis_ranges[key].max}
                    step="0.01"
                    value={value}
                    onChange={(event) => setDraft({
                      ...draft,
                      axes_initial: { ...draft.axes_initial, [key]: Number(event.target.value) }
                    })}
                  />
                </label>
                <div className="axis-range">
                  <label>最小<input type="number" min="0" max={draft.axis_ranges[key].max - 0.01} step="0.01" value={draft.axis_ranges[key].min} onChange={(event) => setDraft({
                    ...draft,
                    axis_ranges: { ...draft.axis_ranges, [key]: { ...draft.axis_ranges[key], min: Number(event.target.value) } }
                  })} /></label>
                  <label>最大<input type="number" min={draft.axis_ranges[key].min + 0.01} max="1" step="0.01" value={draft.axis_ranges[key].max} onChange={(event) => setDraft({
                    ...draft,
                    axis_ranges: { ...draft.axis_ranges, [key]: { ...draft.axis_ranges[key], max: Number(event.target.value) } }
                  })} /></label>
                </div>
              </div>
            ))}
          </div>
          <div className="state-actions">
            <button onClick={saveConfig}>保存参数</button>
            <button onClick={simulateState}>模拟</button>
            <button className="danger" onClick={resetState}>重置状态</button>
          </div>
        </section>
      )}
      <div className="state-columns">
        <section><h3>有效当前事实</h3>{data?.current_items.map((item) => (
          <article key={item.id}><strong>{item.summary}</strong><span>到期 {new Date(item.expires_at).toLocaleString()}</span></article>
        ))}</section>
        <section><h3>跨 profile 连续性</h3>{data?.continuities.map((item) => (
          <article key={item.topic_key}><strong>{item.summary}</strong><span>保留至 {new Date(item.expires_at).toLocaleString()}</span></article>
        ))}</section>
      </div>
      {data?.interaction?.suggestions.map((suggestion) => <p className="suggestion" key={suggestion}>建议 · {suggestion}</p>)}
    </section>
  );
}

function ReportsPanel({ reports }: { reports: ConsolidationReport[] }) {
  return (
    <section className="reports-page">
      <div className="page-intro"><p className="eyebrow">DEFAULT · EVERY 7 DAYS</p><h2>整理报告</h2><p>后台整理结果仅保存在星图，默认不主动外发。</p></div>
      {reports.length === 0 ? <p className="muted">尚无整理报告。</p> : reports.map((report) => (
        <article className="report-card" key={report.id}>
          <div><strong>{new Date(report.period_start).toLocaleDateString()} – {new Date(report.period_end).toLocaleDateString()}</strong><span>生成于 {new Date(report.created_at).toLocaleString()}</span></div>
          <dl><dt>新增证据</dt><dd>{report.summary.evidence_added}</dd><dt>工具确认</dt><dd>{report.summary.tool_results}</dd><dt>敏感脱敏</dt><dd>{report.summary.redactions}</dd><dt>待确认</dt><dd>{report.summary.pending_confirmation}</dd></dl>
        </article>
      ))}
    </section>
  );
}

function VaultPanel({
  entries,
  grants,
  linkedMemory,
  onChange
}: {
  entries: VaultEntry[];
  grants: VaultGrant[];
  linkedMemory: Record<string, string> | null;
  onChange: () => Promise<void>;
}) {
  const [label, setLabel] = useState("");
  const [hint, setHint] = useState("");
  const [secret, setSecret] = useState("");
  const [grantProfile, setGrantProfile] = useState<Record<string, string>>({});

  async function create(event: FormEvent) {
    event.preventDefault();
    await api.createVaultEntry({
      kind: "credential",
      display_label: label,
      redacted_hint: hint,
      secret_value: secret,
      linked_memory_id: linkedMemory?.record_id
    });
    setLabel(""); setHint(""); setSecret("");
    await onChange();
  }

  async function grant(entryId: string) {
    await api.grantVault(entryId, grantProfile[entryId] || "default", 15);
    await onChange();
  }

  async function revoke(grantId: string) {
    await api.revokeVault(grantId);
    await onChange();
  }

  return (
    <section className="vault-page">
      <div className="vault-intro">
        <p className="eyebrow">ENCRYPTED · MANUAL ONLY</p>
        <h2>受保护资源</h2>
        <p>密文与普通记忆隔离。Hermes 只能在你创建了限时、指定 profile 的授权后读取。</p>
      </div>
      <form className="vault-form" onSubmit={create}>
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="显示名称" required />
        <input value={hint} onChange={(e) => setHint(e.target.value)} placeholder="脱敏提示，例如 GitHub …A7F2" required />
        <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="敏感值" required />
        {linkedMemory && <p className="linked">关联到：{linkedMemory.label}</p>}
        <button type="submit">加密保存</button>
      </form>
      <div className="vault-list">
        {entries.map((entry) => (
          <article key={entry.id}>
            <div><span className="lock">◆</span><h3>{entry.display_label}</h3><p>{entry.redacted_hint}</p></div>
            <div className="grant-row">
              <input
                value={grantProfile[entry.id] || ""}
                onChange={(event) => setGrantProfile({ ...grantProfile, [entry.id]: event.target.value })}
                placeholder="Hermes profile"
              />
              <button onClick={() => grant(entry.id)}>
                授权 15 分钟
              </button>
            </div>
          </article>
        ))}
      </div>
      <section className="grant-list">
        <div>
          <p className="eyebrow">ACTIVE GRANTS</p>
          <h2>当前授权</h2>
        </div>
        {grants.length === 0 ? (
          <p className="muted">当前没有可用授权。</p>
        ) : (
          grants.map((grantItem) => (
            <article key={grantItem.id}>
              <div>
                <strong>{grantItem.display_label}</strong>
                <span>Hermes · {grantItem.target_profile}</span>
                <span>到期：{new Date(grantItem.expires_at).toLocaleString()}</span>
              </div>
              <button className="danger" onClick={() => revoke(grantItem.id)}>立即撤销</button>
            </article>
          ))
        )}
      </section>
    </section>
  );
}
