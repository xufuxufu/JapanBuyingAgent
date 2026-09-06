(() => {
  // ---- Address edit toggle ----
  const toggle = document.getElementById("addressEditToggle");
  const form = document.getElementById("addressEditForm");
  const cancel = document.getElementById("addressEditCancel");
  toggle?.addEventListener("click", () => {
    form.hidden = !form.hidden;
  });
  cancel?.addEventListener("click", () => {
    form.hidden = true;
  });
  const quickPick = document.getElementById("addressQuickPick");
  quickPick?.addEventListener("change", () => {
    const option = quickPick.selectedOptions[0];
    if (!option || !option.value) return;
    document.getElementById("addressEditName").value = option.dataset.name || "";
    document.getElementById("addressEditPhone").value = option.dataset.phone || "";
    document.getElementById("addressEditAddress").value = option.dataset.address || "";
  });
})();

(() => {
  // ---- Create-shipment: quantity steppers + select-all + ship-all ----
  const shipmentForm = document.getElementById("createShipmentForm");
  if (!shipmentForm) return;

  shipmentForm.querySelectorAll(".shipment-item-row").forEach((row) => {
    const checkbox = row.querySelector(".shipment-item-checkbox");
    const qtyInput = row.querySelector(".shipment-qty-input");
    const minus = row.querySelector(".shipment-qty-minus");
    const plus = row.querySelector(".shipment-qty-plus");
    const clamp = () => {
      const max = Number(qtyInput.max) || 1;
      let value = Number(qtyInput.value) || 1;
      value = Math.max(1, Math.min(max, value));
      qtyInput.value = value;
    };
    minus.addEventListener("click", () => {
      qtyInput.value = Number(qtyInput.value) - 1;
      clamp();
      checkbox.checked = true;
    });
    plus.addEventListener("click", () => {
      qtyInput.value = Number(qtyInput.value) + 1;
      clamp();
      checkbox.checked = true;
    });
    qtyInput.addEventListener("change", clamp);
    qtyInput.addEventListener("focus", () => {
      checkbox.checked = true;
    });
  });

  document.getElementById("selectAllShipmentItems")?.addEventListener("click", () => {
    shipmentForm.querySelectorAll(".shipment-item-checkbox").forEach((box) => {
      box.checked = true;
    });
  });

  function collectItems({ onlyChecked }) {
    const items = [];
    shipmentForm.querySelectorAll(".shipment-item-row").forEach((row) => {
      const checkbox = row.querySelector(".shipment-item-checkbox");
      if (onlyChecked && !checkbox.checked) return;
      const qtyInput = row.querySelector(".shipment-qty-input");
      const quantity = onlyChecked ? Number(qtyInput.value) : Number(qtyInput.max);
      items.push({ order_item_id: Number(checkbox.dataset.itemId), quantity });
    });
    return items;
  }

  shipmentForm.addEventListener("submit", (event) => {
    const items = collectItems({ onlyChecked: true });
    if (!items.length) {
      event.preventDefault();
      window.alert("请至少选择一个商品");
      return;
    }
    document.getElementById("shipmentItemsJsonInput").value = JSON.stringify(items);
  });

  document.getElementById("shipAllButton")?.addEventListener("click", () => {
    shipmentForm.querySelectorAll(".shipment-item-checkbox").forEach((box) => {
      box.checked = true;
    });
    document.getElementById("shipmentItemsJsonInput").value = JSON.stringify(collectItems({ onlyChecked: false }));
    shipmentForm.requestSubmit();
  });
})();

(() => {
  // ---- Per-shipment label upload: instant local preview (multiple forms share this class-based wiring) ----
  document.querySelectorAll(".shipment-label-form").forEach((form) => {
    const input = form.querySelector(".shipment-label-input");
    const picker = form.querySelector(".tag-photo-picker");
    const submitButton = form.querySelector(".shipment-label-submit");
    const previewWrap = form.querySelector(".shipment-label-preview");
    const previewImage = form.querySelector(".shipment-label-preview-image");
    input.addEventListener("change", () => {
      // Only rewrite the label's leading text node -- picker also contains
      // the (hidden) file input itself as a child, and clobbering the whole
      // label via textContent would delete that input from the DOM.
      if (input.files.length) {
        const file = input.files[0];
        picker.childNodes[0].textContent = `已选择：${file.name}`;
        submitButton.disabled = false;
        // Show what was actually picked immediately -- the user shouldn't
        // have to upload first just to confirm it's the right photo.
        previewImage.src = URL.createObjectURL(file);
        previewWrap.hidden = false;
      } else {
        submitButton.disabled = true;
        previewWrap.hidden = true;
      }
    });
    form.addEventListener("submit", () => {
      submitButton.disabled = true;
      submitButton.textContent = "上传中…";
    });
  });
})();

(() => {
  // ---- Logistics fixup toggle (correcting a mistake after a shipment shipped) ----
  document.querySelectorAll(".logistics-fixup-toggle").forEach((button) => {
    const block = button.nextElementSibling;
    button.addEventListener("click", () => {
      block.hidden = !block.hidden;
    });
  });
})();

(() => {
  // ---- Shipping-label tracking-number scan (拍照/扫码识别运单号) ----
  // One shared camera overlay reused by every shipment's tracking form on
  // this page (pending shipments and the shipped-shipment logistics-fixup
  // block both render a ".tracking-scan-trigger" button next to their own
  // tracking_no input) -- never auto-saves; it only fills the input so the
  // existing "保存物流信息" submit stays the one action that persists it.
  const triggers = document.querySelectorAll(".tracking-scan-trigger");
  const overlay = document.getElementById("trackingScanOverlay");
  if (!triggers.length || !overlay || !window.JBACamera || !window.JBATrackingScan) return;

  const video = document.getElementById("trackingScanVideo");
  const placeholder = document.getElementById("trackingScanPlaceholder");
  const status = document.getElementById("trackingScanStatus");
  const candidatesBox = document.getElementById("trackingScanCandidates");
  const candidateList = document.getElementById("trackingScanCandidateList");
  const cancelButton = document.getElementById("trackingScanCancel");

  const collector = new window.JBATrackingScan.TrackingCandidateCollector();
  let targetInput = null;
  let settleTimer = null;
  let noResultTimer = null;

  const scanner = new window.JBACamera.UnifiedJanScanner({
    video, placeholder, status,
    // Carrier tracking-number barcodes are typically Code128/Code39/ITF, not
    // the JAN-only EAN/UPC formats camera_adapter.js defaults to -- these
    // options (added alongside this feature) widen what gets decoded without
    // touching any JAN-scanning page.
    barcodeFormats: ["code_128", "code_39", "itf", "codabar", "ean_13", "ean_8"],
    zxingFormatNames: ["CODE_128", "CODE_39", "ITF", "CODABAR", "EAN_13", "EAN_8"],
    validJan: (raw) => window.JBATrackingScan.normalizeTrackingCandidate(raw),
    sameCodeDebounceMs: 1200,
    // acceptCode() already ran the raw decode through validJan (the
    // normalizer above), so `value` here is already normalized -- just
    // collect it.
    onCode(value) {
      collector.add(value);
      renderCandidates();
    },
  });

  function clearTimers() {
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    if (noResultTimer) { clearTimeout(noResultTimer); noResultTimer = null; }
  }

  function renderCandidates() {
    const list = collector.list();
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    if (!list.length) return;
    if (list.length === 1) {
      candidatesBox.hidden = true;
      candidateList.replaceChildren();
      status.textContent = `识别到运单号 ${list[0]}，即将自动填入…`;
      // Brief grace window before auto-accepting a single candidate -- a
      // label with more than one barcode may decode a second, different
      // code a moment later, at which point this falls through to the
      // multi-candidate picker below instead of silently keeping the first.
      settleTimer = setTimeout(() => acceptCandidate(list[0]), 700);
      return;
    }
    status.textContent = "识别到多个可能的运单号，请选择：";
    candidatesBox.hidden = false;
    candidateList.replaceChildren();
    list.forEach((code) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "compact tracking-scan-candidate";
      button.textContent = code;
      button.addEventListener("click", () => acceptCandidate(code));
      candidateList.append(button);
    });
  }

  function acceptCandidate(value) {
    clearTimers();
    scanner.stop("tracking_scan_matched");
    overlay.hidden = true;
    candidatesBox.hidden = true;
    candidateList.replaceChildren();
    if (targetInput) {
      // Fill and focus only -- the user still taps the form's own "保存物流
      // 信息" to persist it (never auto-saved from a scan).
      targetInput.value = value;
      targetInput.focus();
    }
    targetInput = null;
  }

  function closeOverlay() {
    clearTimers();
    scanner.stop("tracking_scan_cancel");
    overlay.hidden = true;
    candidatesBox.hidden = true;
    candidateList.replaceChildren();
    targetInput = null;
  }

  async function openOverlay(input) {
    targetInput = input;
    collector.reset();
    candidatesBox.hidden = true;
    candidateList.replaceChildren();
    overlay.hidden = false;
    status.textContent = "正在开启摄像头…";
    noResultTimer = setTimeout(() => {
      if (!collector.list().length) status.textContent = "还未识别到条码，请靠近、对齐运单号条码，或点击“取消，手动输入”。";
    }, 8000);
    try {
      await scanner.start();
      status.textContent = "请将运单号条码对准取景框。";
    } catch (error) {
      const details = window.JBACamera.cameraErrorDetails(error, {
        secureContext: globalThis.isSecureContext, platform: scanner.cameraAdapter.platform, mediaDevices: navigator.mediaDevices,
      });
      status.textContent = `${details.message}可点击“取消，手动输入”改为手工填写。`;
    }
  }

  triggers.forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.target || "");
      if (input) openOverlay(input);
    });
  });

  cancelButton?.addEventListener("click", closeOverlay);
  window.addEventListener("pagehide", () => scanner.stop("pagehide"));
})();
