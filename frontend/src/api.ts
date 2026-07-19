let namespace = "";

function activeNamespace() {
  if (!namespace) throw new Error("UI_NAMESPACE_NOT_CONFIGURED");
  return namespace;
}

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
    untrusted_tool_facts?: number;
    facts: Array<{ fact_type: string; memory_state: string; count: number }>;
    conflicts: unknown[];
  };
};

export type ReviewQueueItem = {
  memory_id: string;
  statement: string;
  fact_type: string;
  state: string;
  source_profile: string;
  confidence: number;
  evidence_count: number;
  updated_at: string;
  extraction_method: string;
  review_reasons: Array<"candidate" | "untrusted_tool">;
  tool_names: string[];
};

export type ReviewQueue = {
  items: ReviewQueueItem[];
  total: number;
  limit: number;
  offset: number;
  profiles: string[];
};

export type QualityReport = {
  namespace: string;
  generated_at: string;
  automatic_ready: boolean;
  promotion_ready: boolean;
  manual_review_required: boolean;
  gates: Record<string, boolean>;
  metrics: Record<string, number | null>;
  classifications: Record<string, number>;
  decision: "AUTOMATIC_GATES_FAILED" | "MANUAL_REVIEW_REQUIRED";
};

export function context(profile = "star-map") {
  const id = crypto.randomUUID();
  return {
    shared_namespace: activeNamespace(),
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
  configure: async () => {
    const config = await request<{ namespace: string; version: string }>("/api/v1/ui/config");
    namespace = config.namespace;
    return config;
  },
  graph: () =>
    request<GraphData>(
      `/api/v1/graph/subgraph?shared_namespace=${encodeURIComponent(activeNamespace())}`
    ),
  updateSubject: (
    subjectId: string,
    displayName: string,
    color: string,
    reason: string
  ) => request(`/api/v1/graph/subjects/${subjectId}`, {
    method: "PUT",
    body: JSON.stringify({
      context: context(),
      display_name: displayName,
      color,
      reason
    })
  }),
  trace: (memoryId: string) =>
    request<Record<string, unknown>>(
      `/api/v1/memory/${memoryId}/trace?shared_namespace=${encodeURIComponent(activeNamespace())}`
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
  mergeEntity: (entityId: string, targetEntityId: string, reason: string) =>
    request(`/api/v1/entities/${entityId}/merge`, {
      method: "POST",
      body: JSON.stringify({
        context: context(),
        target_entity_id: targetEntityId,
        reason
      })
    }),
  unmergeEntity: (entityId: string, reason: string) =>
    request(`/api/v1/entities/${entityId}/unmerge`, {
      method: "POST",
      body: JSON.stringify({ context: context(), reason })
    }),
  splitEntity: (
    entityId: string,
    canonicalName: string,
    entityType: string,
    factIds: string[],
    reason: string
  ) => request(`/api/v1/entities/${entityId}/split`, {
    method: "POST",
    body: JSON.stringify({
      context: context(),
      canonical_name: canonicalName,
      entity_type: entityType,
      fact_ids: factIds,
      reason
    })
  }),
  changeEntityRelation: (
    entityId: string,
    factId: string,
    action: "attach" | "detach",
    reason: string
  ) => request(`/api/v1/entities/${entityId}/facts/${factId}/${action}`, {
    method: "POST",
    body: JSON.stringify({ context: context(), reason })
  }),
  vaultEntries: () =>
    request<VaultEntry[]>(
      `/api/v1/vault/entries?shared_namespace=${encodeURIComponent(activeNamespace())}`
    ),
  vaultGrants: () =>
    request<VaultGrant[]>(
      `/api/v1/vault/grants?shared_namespace=${encodeURIComponent(activeNamespace())}`
    ),
  state: () =>
    request<StateData>(
      `/api/v1/state?shared_namespace=${encodeURIComponent(activeNamespace())}`
    ),
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
      `/api/v1/reports/consolidation?shared_namespace=${encodeURIComponent(activeNamespace())}`
    ),
  qualityReport: () =>
    request<QualityReport>(
      `/api/v1/reports/quality?shared_namespace=${encodeURIComponent(activeNamespace())}`
    ),
  reviewQueue: (options: {
    reason?: "all" | "candidate" | "untrusted_tool";
    sourceProfile?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const parameters = new URLSearchParams({
      shared_namespace: activeNamespace(),
      reason: options.reason || "all",
      limit: String(options.limit || 50),
      offset: String(options.offset || 0)
    });
    if (options.sourceProfile) parameters.set("source_profile", options.sourceProfile);
    return request<ReviewQueue>(`/api/v1/memories/review?${parameters.toString()}`);
  },
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
  revealVaultEntry: (entryId: string, password: string) =>
    request<{ entry_id: string; secret_value: string }>(
      `/api/v1/vault/entries/${entryId}/reveal`,
      {
        method: "POST",
        body: JSON.stringify({
          context: context(), password, reason: "User manually revealed the Vault entry"
        })
      }
    ),
  updateVaultEntry: (
    entryId: string,
    displayLabel: string,
    redactedHint: string,
    password: string
  ) => request(`/api/v1/vault/entries/${entryId}`, {
    method: "PATCH",
    body: JSON.stringify({
      context: context(),
      display_label: displayLabel,
      redacted_hint: redactedHint,
      password,
      reason: "User updated Vault metadata"
    })
  }),
  replaceVaultSecret: (entryId: string, secretValue: string, password: string) =>
    request(`/api/v1/vault/entries/${entryId}/replace`, {
      method: "POST",
      body: JSON.stringify({
        context: context(),
        secret_value: secretValue,
        password,
        reason: "User replaced the Vault secret"
      })
    }),
  setVaultEntryStatus: (entryId: string, status: "active" | "disabled", password: string) =>
    request(`/api/v1/vault/entries/${entryId}/status`, {
      method: "POST",
      body: JSON.stringify({
        context: context(), status, password, reason: `User set Vault entry to ${status}`
      })
    }),
  deleteVaultEntry: (entryId: string, password: string) =>
    request(`/api/v1/vault/entries/${entryId}/delete`, {
      method: "POST",
      body: JSON.stringify({
        context: context(),
        confirm_entry_id: entryId,
        password,
        reason: "User permanently deleted the Vault entry"
      })
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
