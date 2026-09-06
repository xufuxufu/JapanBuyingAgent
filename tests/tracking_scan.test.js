// Node built-in test runner -- no npm dependency, run with:
//   node --test tests/tracking_scan.test.js
// Pure logic (no DOM/camera APIs), covering the shipping-label tracking-
// number scan feature's candidate normalization/collection rules that back
// sales_order_detail.js's scan UI.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { normalizeTrackingCandidate, TrackingCandidateCollector } = require(
  path.join(__dirname, "..", "app", "static", "tracking_scan.js"),
);

test("normalizeTrackingCandidate accepts a plausible all-digit tracking number", () => {
  assert.equal(normalizeTrackingCandidate("781234567890"), "781234567890");
});

test("normalizeTrackingCandidate uppercases and strips internal whitespace", () => {
  assert.equal(normalizeTrackingCandidate(" sf 123 456 789 "), "SF123456789");
});

test("normalizeTrackingCandidate rejects values that are too short", () => {
  assert.equal(normalizeTrackingCandidate("12345"), null);
});

test("normalizeTrackingCandidate rejects values that are too long", () => {
  assert.equal(normalizeTrackingCandidate("1".repeat(41)), null);
});

test("normalizeTrackingCandidate rejects punctuation/garbage from a misread barcode", () => {
  assert.equal(normalizeTrackingCandidate("!!not-a-code??"), null);
});

test("normalizeTrackingCandidate rejects empty/null/undefined", () => {
  assert.equal(normalizeTrackingCandidate(""), null);
  assert.equal(normalizeTrackingCandidate(null), null);
  assert.equal(normalizeTrackingCandidate(undefined), null);
});

test("normalizeTrackingCandidate allows hyphens (some carriers format numbers this way)", () => {
  assert.equal(normalizeTrackingCandidate("ZT-2026-000123"), "ZT-2026-000123");
});

test("TrackingCandidateCollector dedupes the same code decoded repeatedly", () => {
  const collector = new TrackingCandidateCollector();
  collector.add("781234567890");
  collector.add("781234567890");
  collector.add("781234567890");
  assert.deepEqual(collector.list(), ["781234567890"]);
});

test("TrackingCandidateCollector keeps first-seen order across distinct codes", () => {
  const collector = new TrackingCandidateCollector();
  collector.add("SECONDCODE01");
  collector.add("FIRSTCODE001");
  collector.add("SECONDCODE01");
  assert.deepEqual(collector.list(), ["SECONDCODE01", "FIRSTCODE001"]);
});

test("TrackingCandidateCollector ignores shape-invalid input without affecting existing candidates", () => {
  const collector = new TrackingCandidateCollector();
  collector.add("VALIDCODE001");
  assert.equal(collector.add("bad"), null);
  assert.deepEqual(collector.list(), ["VALIDCODE001"]);
});

test("TrackingCandidateCollector caps how many distinct candidates it stores", () => {
  const collector = new TrackingCandidateCollector({maxCandidates: 2});
  assert.equal(collector.add("CODEONE0001"), "CODEONE0001");
  assert.equal(collector.add("CODETWO0002"), "CODETWO0002");
  // A third distinct code is still recognized (returned) but not stored --
  // the cap only bounds how many choices get shown to the user.
  assert.equal(collector.add("CODETHREE03"), "CODETHREE03");
  assert.deepEqual(collector.list(), ["CODEONE0001", "CODETWO0002"]);
});

test("TrackingCandidateCollector.reset clears accumulated candidates for a new scan session", () => {
  const collector = new TrackingCandidateCollector();
  collector.add("SOMECODE001");
  collector.reset();
  assert.deepEqual(collector.list(), []);
});
