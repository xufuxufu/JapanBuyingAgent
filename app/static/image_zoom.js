// Reusable lightweight image-zoom component: click any element with class
// "zoomable" (using its data-zoom-src, falling back to src) to see a
// full-screen overlay of the image. Click the overlay (image or background)
// again, or press Esc, to close. Deliberately minimal: no prev/next
// navigation, no toolbar, no close button to hunt for -- mobile-first.
(() => {
  let overlay = null;
  let overlayImage = null;

  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.className = "image-zoom-overlay";
    overlay.hidden = true;
    overlayImage = document.createElement("img");
    overlayImage.className = "image-zoom-overlay-image";
    overlay.appendChild(overlayImage);
    overlay.addEventListener("click", closeOverlay);
    document.body.appendChild(overlay);
    return overlay;
  }

  function openOverlay(src) {
    ensureOverlay();
    overlayImage.src = src;
    overlay.hidden = false;
  }

  function closeOverlay() {
    if (overlay) overlay.hidden = true;
  }

  // Capture phase, deliberately: a zoomable thumbnail is usually nested
  // inside a clickable row/card (e.g. "select this product"). A bubble-phase
  // listener here would fire AFTER that ancestor's own bubble-phase click
  // handler already ran, so stopPropagation() would be too late to prevent
  // it. Capture runs top-down before any bubble-phase handler, so stopping
  // it here reliably suppresses the ancestor's click as well.
  document.addEventListener("click", (event) => {
    const target = event.target.closest(".zoomable");
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    const src = target.dataset.zoomSrc || target.currentSrc || target.src;
    if (src) openOverlay(src);
  }, true);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeOverlay();
  });
})();
