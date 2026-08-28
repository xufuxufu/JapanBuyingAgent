(() => {
  const form = document.getElementById("purchaseForm");
  if (!form) return;

  const submitButton = document.getElementById("purchaseSubmit");
  const countLabel = document.getElementById("purchaseSelectedCount");
  const executionsInput = document.getElementById("purchaseExecutionsInput");
  if (!submitButton) return;

  const cards = [...form.querySelectorAll(".pd-purchase-card")];

  function updateBar() {
    const checked = cards.filter((card) => card.querySelector(".pd-purchase-checkbox")?.checked);
    countLabel.textContent = String(checked.length);
    submitButton.disabled = checked.length === 0;
  }

  cards.forEach((card) => {
    const checkbox = card.querySelector(".pd-purchase-checkbox");
    const qtyInput = card.querySelector(".pd-purchase-qty-input");
    const hint = card.querySelector(".pd-overbuy-hint");
    const remaining = Number(card.dataset.remaining) || 0;

    function refreshHint() {
      const quantity = Number(qtyInput.value) || 0;
      if (quantity > remaining) {
        hint.textContent = `超过计划剩余${remaining}件，本次${quantity}件（仍可提交）`;
        hint.hidden = false;
      } else {
        hint.hidden = true;
      }
    }

    checkbox?.addEventListener("change", updateBar);
    qtyInput?.addEventListener("input", () => {
      if (Number(qtyInput.value) > 0) checkbox.checked = true;
      refreshHint();
      updateBar();
    });
    refreshHint();
  });

  form.addEventListener("submit", (event) => {
    const executions = [];
    for (const card of cards) {
      const checkbox = card.querySelector(".pd-purchase-checkbox");
      if (!checkbox?.checked) continue;
      const qtyInput = card.querySelector(".pd-purchase-qty-input");
      const quantity = Number(qtyInput.value);
      if (!Number.isFinite(quantity) || quantity <= 0) {
        event.preventDefault();
        window.alert("实际采购数量必须大于0");
        return;
      }
      const storeSelect = card.querySelector(".pd-purchase-store-select");
      if (storeSelect && !storeSelect.value) {
        event.preventDefault();
        window.alert("请为每个勾选的商品选择门店");
        return;
      }
      executions.push({
        plan_id: Number(card.dataset.planId), quantity,
        store_id: storeSelect ? Number(storeSelect.value) : null,
      });
    }
    if (!executions.length) {
      event.preventDefault();
      return;
    }
    executionsInput.value = JSON.stringify(executions);
    submitButton.disabled = true;
  });

  updateBar();
})();
