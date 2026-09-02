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
