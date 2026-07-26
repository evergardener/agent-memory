import assert from "node:assert/strict";
import test from "node:test";
import {
  presentRelation,
  presentRelations,
  relationMatchesOverlay,
  subjectOrbitLayout
} from "./starMapModel.ts";

function subject(id, label, kind) {
  return { data: { id, record_id: id, label, kind: "subject", subject_kind: kind } };
}

test("the user anchors the center while profiles share a stable orbit", () => {
  const layout = subjectOrbitLayout([
    subject("qishuo", "qishuo", "profile_persona"),
    subject("user", "User", "user"),
    subject("jiuyue", "jiuyue", "profile_persona"),
    subject("release", "release-gate", "profile_persona")
  ]);
  const center = layout.find((item) => item.node.data.id === "user");
  const profiles = layout.filter((item) => item.node.data.id !== "user");

  assert.deepEqual(center && { x: center.x, y: center.y }, { x: 0, y: 0 });
  const radii = profiles.map((item) => Math.hypot(item.x, item.y));
  assert.ok(radii.every((radius) => Math.abs(radius - radii[0]) < 0.001));
});

test("two profiles sit opposite each other around the user", () => {
  const layout = subjectOrbitLayout([
    subject("user", "User", "user"),
    subject("jiuyue", "jiuyue", "profile_persona"),
    subject("qishuo", "qishuo", "profile_persona")
  ]);
  const profiles = layout.filter((item) => item.node.data.id !== "user");

  assert.equal(profiles[0].y, 0);
  assert.ok(Math.abs(profiles[1].y) < 0.001);
  assert.equal(profiles[0].x, -profiles[1].x);
});

test("episode bridges count shared episodes instead of missing evidence ids", () => {
  const presented = presentRelation(
    {
      data: {
        id: "episode-relation:qishuo:user",
        source: "qishuo",
        target: "user",
        kind: "episode_relation",
        support_count: "2",
        episode_ids: "episode:one|episode:two"
      }
    },
    new Map([["qishuo", "qishuo"], ["user", "User"]]),
    {}
  );

  assert.equal(presented.data.label, "qishuo · 共同情节 · User");
  assert.equal(presented.data.episode_count, "2");
  assert.equal(presented.data.bridge_label, "共同情节 · 2");
  assert.equal(presented.data.evidence_count, "");
});

test("durable relations retain their supporting fact and episode count", () => {
  const presented = presentRelation(
    {
      data: {
        id: "relationship:user:a",
        source: "user",
        target: "a",
        kind: "relationship",
        relation_type: "friend",
        fact_ids: "fact:one",
        episode_ids: "episode:one"
      }
    },
    new Map([["user", "User"], ["a", "小A"]]),
    { friend: "朋友" }
  );

  assert.equal(presented.data.label, "User — 朋友 → 小A");
  assert.equal(presented.data.evidence_count, "2");
  assert.equal(presented.data.episode_count, "");
});

test("duplicate durable relations render as one edge with combined support", () => {
  const presented = presentRelations(
    [
      {
        data: {
          id: "relationship:two",
          record_id: "two",
          source: "user",
          target: "a",
          kind: "relationship",
          relation_type: "friend",
          episode_ids: "episode:two"
        }
      },
      {
        data: {
          id: "relationship:one",
          record_id: "one",
          source: "user",
          target: "a",
          kind: "relationship",
          relation_type: "friend",
          fact_ids: "fact:one"
        }
      }
    ],
    new Map([["user", "User"], ["a", "小A"]]),
    { friend: "朋友" }
  );

  assert.equal(presented.length, 1);
  assert.equal(presented[0].data.id, "relationship:one");
  assert.equal(presented[0].data.record_ids, "one|two");
  assert.equal(presented[0].data.evidence_count, "2");
});

test("overlay animation can follow episode support or participant relations", () => {
  assert.equal(
    relationMatchesOverlay(
      {
        source: "user",
        target: "chengdu",
        episode_ids: "episode:trip"
      },
      {
        id: "episode:trip",
        kind: "episode",
        entity_ids: "chengdu",
        subject_ids: "user"
      }
    ),
    true
  );
  assert.equal(
    relationMatchesOverlay(
      {
        source: "user",
        target: "chengdu"
      },
      {
        id: "fact:trip",
        kind: "fact",
        entity_ids: "chengdu",
        subject_ids: "user"
      }
    ),
    true
  );
  assert.equal(
    relationMatchesOverlay(
      {
        source: "qishuo",
        target: "n8n"
      },
      {
        id: "episode:trip",
        kind: "episode",
        entity_ids: "chengdu",
        subject_ids: "user"
      }
    ),
    false
  );
});
