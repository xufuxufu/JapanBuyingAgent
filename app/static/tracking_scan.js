(function trackingScanModule(global) {
  "use strict";

  // Shipping-label barcodes are carrier tracking numbers, not JAN product
  // codes -- no checksum to validate against, so this is a shape check only:
  // reasonably long, no whitespace, letters/digits/hyphens (covers 中通's
  // all-digit numbers and other carriers' alphanumeric ones alike).
  const MIN_LENGTH = 6;
  const MAX_LENGTH = 40;
  const SHAPE_PATTERN = /^[A-Z0-9-]+$/;

  function normalizeTrackingCandidate(raw) {
    const value = String(raw == null ? "" : raw).trim().toUpperCase().replace(/\s+/g, "");
    if (value.length < MIN_LENGTH || value.length > MAX_LENGTH) return null;
    if (!SHAPE_PATTERN.test(value)) return null;
    return value;
  }

  // Collects distinct decoded candidates in first-seen order while a scan
  // session runs. A shipping label often carries more than one barcode
  // (JAN of the enclosed goods, insurance/order code, the actual tracking
  // number) -- this never guesses which one is "the" tracking number; it
  // just remembers what was seen so the caller can auto-fill a single
  // candidate or ask the user to pick among several.
  class TrackingCandidateCollector {
    constructor(options = {}) {
      this.maxCandidates = Number(options.maxCandidates || 5);
      this.candidates = [];
    }

    // Returns the normalized value if it's shape-valid, else null.
    // A value beyond maxCandidates is still recognized (returned) but not
    // stored -- the cap only bounds how many distinct choices are shown.
    add(raw) {
      const value = normalizeTrackingCandidate(raw);
      if (!value) return null;
      if (!this.candidates.includes(value) && this.candidates.length < this.maxCandidates) {
        this.candidates.push(value);
      }
      return value;
    }

    list() {
      return [...this.candidates];
    }

    reset() {
      this.candidates = [];
    }
  }

  const api = Object.freeze({
    normalizeTrackingCandidate,
    TrackingCandidateCollector,
  });
  global.JBATrackingScan = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
