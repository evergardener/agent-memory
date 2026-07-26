import type { GraphElement } from "./api";

function splitIds(value = "") {
  return value.split("|").filter(Boolean);
}

function mergeIds(...values: string[]) {
  return [...new Set(values.flatMap(splitIds))].sort().join("|");
}

export function subjectOrbitLayout(nodes: GraphElement[]) {
  const ordered = [...nodes].sort((left, right) => {
    const leftRank = left.data.subject_kind === "user" ? 0 : 1;
    const rightRank = right.data.subject_kind === "user" ? 0 : 1;
    return leftRank - rightRank || left.data.label.localeCompare(right.data.label);
  });
  if (ordered.length === 0) return [];
  const user = ordered.find((node) => node.data.subject_kind === "user");
  if (!user) {
    if (ordered.length === 1) return [{ node: ordered[0], x: 0, y: 0 }];
    const radius = Math.min(190, 72 + ordered.length * 14);
    return ordered.map((node, index) => {
      const angle = -Math.PI / 2 + (index * Math.PI * 2) / ordered.length;
      return { node, x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
    });
  }
  const profiles = ordered.filter((node) => node !== user);
  const positions = [{ node: user, x: 0, y: 0 }];
  if (profiles.length === 0) return positions;
  if (profiles.length === 1) {
    positions.push({ node: profiles[0], x: 132, y: 0 });
    return positions;
  }
  if (profiles.length > 8) {
    profiles.forEach((node, index) => {
      const ring = Math.floor(index / 8);
      const ringStart = ring * 8;
      const ringCount = Math.min(8, profiles.length - ringStart);
      const angle = -Math.PI / 2 + ((index - ringStart) * Math.PI * 2) / ringCount;
      positions.push({
        node,
        x: Math.cos(angle) * (112 + ring * 78),
        y: Math.sin(angle) * (76 + ring * 54)
      });
    });
    return positions;
  }
  const radius = Math.min(190, 104 + profiles.length * 10);
  const startAngle = profiles.length === 2 ? 0 : -Math.PI / 2;
  profiles.forEach((node, index) => {
    const angle = startAngle + (index * Math.PI * 2) / profiles.length;
    positions.push({ node, x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
  });
  return positions;
}

export function presentRelation(
  edge: GraphElement,
  celestialLabels: Map<string, string>,
  relationLabels: Record<string, string>
): GraphElement {
  const sourceLabel = celestialLabels.get(edge.data.source) || "天体";
  const targetLabel = celestialLabels.get(edge.data.target) || "天体";
  const episodeCount = Math.max(
    Number(edge.data.support_count || 0),
    splitIds(edge.data.episode_ids).length
  );
  const episodeRelation = edge.data.kind === "episode_relation";
  const durableEvidenceCount = Math.max(
    Number(edge.data.evidence_count || 0),
    new Set([
      ...splitIds(edge.data.evidence_ids),
      ...splitIds(edge.data.fact_ids),
      ...splitIds(edge.data.episode_ids)
    ]).size
  );
  return {
    ...edge,
    data: {
      ...edge.data,
      label: episodeRelation
        ? `${sourceLabel} · 共同情节 · ${targetLabel}`
        : `${sourceLabel} — ${
          edge.data.label ||
          relationLabels[edge.data.relation_type] ||
          "关联"
        } → ${targetLabel}`,
      evidence_count: episodeRelation
        ? ""
        : String(durableEvidenceCount),
      episode_count: episodeRelation ? String(episodeCount) : "",
      bridge_label: episodeRelation ? `共同情节 · ${episodeCount}` : ""
    }
  };
}

export function presentRelations(
  edges: GraphElement[],
  celestialLabels: Map<string, string>,
  relationLabels: Record<string, string>
) {
  const grouped = new Map<string, GraphElement>();
  [...edges]
    .sort((left, right) => left.data.id.localeCompare(right.data.id))
    .forEach((edge) => {
      const episodeRelation = edge.data.kind === "episode_relation";
      const endpoints = episodeRelation
        ? [edge.data.source, edge.data.target].sort()
        : [edge.data.source, edge.data.target];
      const key = [
        edge.data.kind,
        ...endpoints,
        episodeRelation ? "" : edge.data.relation_type || edge.data.label || ""
      ].join("|");
      const existing = grouped.get(key);
      if (!existing) {
        grouped.set(key, {
          ...edge,
          data: {
            ...edge.data,
            record_ids: mergeIds(edge.data.record_ids, edge.data.record_id),
            fact_ids: mergeIds(edge.data.fact_ids),
            episode_ids: mergeIds(edge.data.episode_ids),
            evidence_ids: mergeIds(edge.data.evidence_ids)
          }
        });
        return;
      }
      const episodeIds = mergeIds(existing.data.episode_ids, edge.data.episode_ids);
      grouped.set(key, {
        ...existing,
        data: {
          ...existing.data,
          record_ids: mergeIds(
            existing.data.record_ids,
            existing.data.record_id,
            edge.data.record_ids,
            edge.data.record_id
          ),
          fact_ids: mergeIds(existing.data.fact_ids, edge.data.fact_ids),
          episode_ids: episodeIds,
          evidence_ids: mergeIds(existing.data.evidence_ids, edge.data.evidence_ids),
          support_count: episodeRelation
            ? String(Math.max(
              Number(existing.data.support_count || 0),
              Number(edge.data.support_count || 0),
              splitIds(episodeIds).length
            ))
            : existing.data.support_count
        }
      });
    });
  return [...grouped.values()].map((edge) =>
    presentRelation(edge, celestialLabels, relationLabels)
  );
}
