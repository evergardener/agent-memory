export const NAMESPACE = "hermes:user-primary";

export type GraphData = {
  nodes: Array<{ data: Record<string, string> }>;
  edges: Array<{ data: Record<string, string> }>;
};

export type VaultEntry = {
  id: string;
  kind: string;
  display_label: string;
  redacted_hint: string;
  status: string;
  created_at: string;
  updated_at: string;
};

export type VaultGrant = {
  id: string;
  entry_id: string;
  display_label: string;
  operation: string;
  target_profile: string;
  expires_at: string;
  created_at: string;
};

export type StateData = {
  interaction: null | {
    axes: Record<string, number>;
    summary: string;
    suggestions: string[];
    calculated_at: string;
    algorithm_version: string;
  };
  current_items: Array<{
    id: string;
    summary: string;
    expires_at: string;
    status: string;
  }>;
  continuities: Array<{
    topic_key: string;
    summary: string;
    last_active_at: string;
    expires_at: string;
  }>;
  config: StateConfig;
};

export type StateConfig = {
  enabled: boolean;
  drift_hours: number;
  axes_initial: Record<string, number>;
  axis_labels: Record<string, string>;
  axis_ranges: Record<string, { min: number; max: number }>;
  axis_enabled: Record<string, boolean>;
  thresholds: Record<string, number>;
  profile_overrides: Record<string, Record<string, unknown>>;
  updated_at?: string;
};

export type ConsolidationReport = {
  id: string;
  period_start: string;
  period_end: string;
  created_at: string;
  summary: {
    evidence_added: number;
    tool_results: number;
    redactions: number;
    pending_confirmation: number;
    facts: Array<{ fact_type: string; memory_state: string; count: number }>;
    conflicts: unknown[];
  };
};

export function context(profile = "star-map") {
  const id = crypto.randomUUID();
  return {
    shared_namespace: NAMESPACE,
    source_profile: profile,
    source_instance: "web-ui",
    external_session_id: `ui-${id}`,
    external_turn_id: id,
    correlation_id: id
  };
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    ...options,
    headers: options.body
      ? { "Content-Type": "application/json", ...(options.headers || {}) }
      : options.headers
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail || `HTTP ${response.status}`) as Error & {
      status: number;
    };
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export const api = {
  login: (password: string) =>
    request<{ authenticated: boolean }>("/api/v1/ui/login", {
      method: "POST",
      body: JSON.stringify({ password })
    }),
  logout: () => request("/api/v1/ui/logout", { method: "POST" }),
  graph: () =>
    request<GraphData>(
      `/api/v1/graph/subgraph?shared_namespace=${encodeURIComponent(NAMESPACE)}`
    ),
  trace: (memoryId: string) =>
    request<Record<string, unknown>>(
      `/api/v1/memory/${memoryId}/trace?shared_namespace=${encodeURIComponent(NAMESPACE)}`
    ),
  correct: (memoryId: string, corrected_statement: string, reason: string) =>
    request(`/api/v1/memory/${memoryId}/corrections`, {
      method: "POST",
      body: JSON.stringify({ context: context(), corrected_statement, reason })
    }),
  changeState: (memoryId: string, action: "forget" | "isolate", reason: string) =>
    request(`/api/v1/memory/${memoryId}/${action}`, {
      method: "POST",
      body: JSON.stringify({ context: context(), reason })
    }),
  purge: (memoryId: string, reason: string) =>
    request(`/api/v1/memory/${memoryId}/purge`, {
      method: "POST",
      body: JSON.stringify({
        context: context(),
        reason,
        confirm_memory_id: memoryId
      })
    }),
  vaultEntries: () =>
    request<VaultEntry[]>(
      `/api/v1/vault/entries?shared_namespace=${encodeURIComponent(NAMESPACE)}`
    ),
  vaultGrants: () =>
    request<VaultGrant[]>(
      `/api/v1/vault/grants?shared_namespace=${encodeURIComponent(NAMESPACE)}`
    ),
  state: () =>
    request<StateData>(`/api/v1/state?shared_namespace=${encodeURIComponent(NAMESPACE)}`),
  configureState: (config: StateConfig) =>
    request<StateConfig>("/api/v1/state/config", {
      method: "PUT",
      body: JSON.stringify({
        context: context(),
        ...config,
        reason: "User updated deterministic state settings from the star map"
      })
    }),
  resetState: () =>
    request("/api/v1/state/reset", {
      method: "POST",
      body: JSON.stringify({
        context: context(),
        reason: "User reset deterministic state from the star map"
      })
    }),
  simulateState: (content: string) =>
    request<{ axes: Record<string, number>; summary: string; suggestions: string[] }>(
      "/api/v1/state/simulate",
      {
        method: "POST",
        body: JSON.stringify({
          context: context(),
          event_type: "user_message",
          content
        })
      }
    ),
  reports: () =>
    request<ConsolidationReport[]>(
      `/api/v1/reports/consolidation?shared_namespace=${encodeURIComponent(NAMESPACE)}`
    ),
  createVaultEntry: (payload: {
    kind: string;
    display_label: string;
    redacted_hint: string;
    secret_value: string;
    linked_memory_id?: string;
  }) =>
    request<{ entry_id: string }>("/api/v1/vault/entries", {
      method: "POST",
      body: JSON.stringify({ context: context(), ...payload })
    }),
  grantVault: (entryId: string, targetProfile: string, minutes: number) =>
    request(`/api/v1/vault/entries/${entryId}/grants`, {
      method: "POST",
      body: JSON.stringify({
        context: context(),
        operation: "reveal_to_model",
        target_profile: targetProfile,
        expires_at: new Date(Date.now() + minutes * 60_000).toISOString(),
        reason: "User granted access from the star map"
      })
    }),
  revokeVault: (grantId: string) =>
    request(`/api/v1/vault/grants/${grantId}/revoke`, {
      method: "POST",
      body: JSON.stringify({
        context: context(),
        reason: "User revoked access from the star map"
      })
    })
};
