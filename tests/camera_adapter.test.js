// Node built-in test runner -- no npm dependency, run with:
//   node --test tests/camera_adapter.test.js
// This project's automated `pytest` suite does not execute browser
// MediaStream APIs; camera_adapter.js is already written so its camera
// lifecycle (CameraAdapter/UnifiedJanScanner) can be driven headlessly with
// injected fakes for navigator.mediaDevices/storage/video, which is what
// these tests exercise directly.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { CameraAdapter, UnifiedJanScanner } = require(
  path.join(__dirname, "..", "app", "static", "camera_adapter.js"),
);

function makeTrack(overrides = {}) {
  return {
    stopCalls: 0,
    stop() { this.stopCalls += 1; },
    getSettings() { return { deviceId: overrides.deviceId || "cam-1", zoom: 1 }; },
    getCapabilities() { return overrides.capabilities || {}; },
    async applyConstraints() {},
  };
}

function makeStream(track) {
  return {
    getTracks: () => [track],
    getVideoTracks: () => [track],
  };
}

function makeMediaDevices({ track } = {}) {
  const activeTrack = track || makeTrack();
  const stream = makeStream(activeTrack);
  return {
    calls: [],
    lastTrack: activeTrack,
    async getUserMedia(constraints) {
      this.calls.push(constraints);
      return stream;
    },
    async enumerateDevices() {
      return [{ kind: "videoinput", deviceId: "cam-1", label: "Back Camera" }];
    },
  };
}

function makeVideo() {
  return {
    readyState: 4,
    videoWidth: 640,
    videoHeight: 480,
    srcObject: null,
    classList: { add() {}, remove() {} },
    addEventListener() {},
    removeEventListener() {},
    async play() {},
  };
}

test("first start requests the camera exactly once", async () => {
  const mediaDevices = makeMediaDevices();
  const adapter = new CameraAdapter({ mediaDevices, secureContext: true, storage: null });

  await adapter.start("", { onStream: async () => {} });

  assert.equal(mediaDevices.calls.length, 1);
  assert.equal(adapter.track, mediaDevices.lastTrack);
});

test("pauseAfterSuccess halts the decode loop without touching the camera stream", async () => {
  const mediaDevices = makeMediaDevices();
  const video = makeVideo();
  const scanner = new UnifiedJanScanner({
    video,
    onCode() {},
    cameraOptions: { mediaDevices, secureContext: true, storage: null },
  });
  // Skip the barcode-detector/ZXing decode setup entirely -- these tests are
  // about the camera session lifecycle, not decoding, and neither detector
  // is available in a plain Node process. Fake a live stream/track directly,
  // mirroring what a real cameraAdapter.start() would have populated.
  const track = mediaDevices.lastTrack;
  scanner.stream = mediaDevices ? makeStream(track) : null;
  scanner.track = track;
  scanner.scanning = true;

  scanner.pauseAfterSuccess(100);

  assert.equal(scanner.scanning, false);
  assert.equal(mediaDevices.calls.length, 0, "pausing must not call getUserMedia at all");
  assert.equal(track.stopCalls, 0, "pausing must not stop the existing track");
  assert.notEqual(scanner.stream, null, "the stream stays alive across a pause");
});

test("stop() releases every track from the active stream", async () => {
  const mediaDevices = makeMediaDevices();
  const adapter = new CameraAdapter({ mediaDevices, secureContext: true, storage: null });
  await adapter.start("", { onStream: async () => {} });
  const track = adapter.track;

  adapter.stop();

  assert.equal(track.stopCalls, 1);
  assert.equal(adapter.track, null);
  assert.equal(adapter.stream, null);
});

test("starting again after an explicit stop re-requests the camera and does not leak the old track", async () => {
  const mediaDevices = makeMediaDevices();
  const adapter = new CameraAdapter({ mediaDevices, secureContext: true, storage: null });
  await adapter.start("", { onStream: async () => {} });
  const firstTrack = adapter.track;
  adapter.stop();

  await adapter.start("", { onStream: async () => {} });

  assert.equal(mediaDevices.calls.length, 2, "an explicit stop+start cycle is a real new session, one call each");
  assert.equal(firstTrack.stopCalls, 1, "the first track must already have been released by stop()");
});

test("calling start() twice without an explicit stop never leaves two live tracks running", async () => {
  // Defensive: even if a caller forgets the page-level guard that normally
  // prevents this (see price_check.html's start-button handler), the
  // adapter itself must not end up holding two simultaneously-open streams.
  const mediaDevices = makeMediaDevices();
  const adapter = new CameraAdapter({ mediaDevices, secureContext: true, storage: null });
  await adapter.start("", { onStream: async () => {} });
  const firstTrack = adapter.track;

  await adapter.start("", { onStream: async () => {} });

  assert.equal(firstTrack.stopCalls, 1, "the previous track must be stopped before a new one opens");
});
