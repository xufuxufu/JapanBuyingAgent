(() => {
  const form = document.getElementById("demandPlanForm");
  if (!form) return;

  const submitButton = document.getElementById("demandPlanSubmit");
  const countLabel = document.getElementById("demandPlanCount");
  const selectionsInput = document.getElementById("demandSelectionsInput");
  if (!submitButton) return;

  const cards = [...form.querySelectorAll(".pd-card[data-kind]")];

  function updateBar() {
    const checked = cards.filter((card) => card.querySelector(".pd-checkbox")?.checked);
    countLabel.textContent = String(checked.length);
    submitButton.disabled = checked.length === 0;
  }

  cards.forEach((card) => {
    const checkbox = card.querySelector(".pd-checkbox");
    const qtyInput = card.querySelector(".pd-qty-input");
    checkbox?.addEventListener("change", updateBar);
    qtyInput?.addEventListener("input", () => {
      if (Number(qtyInput.value) > 0) checkbox.checked = true;
      updateBar();
    });
  });

  form.addEventListener("submit", (event) => {
    const selections = [];
    for (const card of cards) {
      const checkbox = card.querySelector(".pd-checkbox");
      if (!checkbox?.checked) continue;
      const qtyInput = card.querySelector(".pd-qty-input");
      const quantity = Number(qtyInput.value);
      if (!Number.isFinite(quantity) || quantity <= 0) {
        event.preventDefault();
        window.alert("计划采购数量必须大于0");
        return;
      }
      selections.push({ kind: card.dataset.kind, key: card.dataset.key, planned_quantity: quantity });
    }
    if (!selections.length) {
      event.preventDefault();
      return;
    }
    selectionsInput.value = JSON.stringify(selections);
    submitButton.disabled = true;
  });

  updateBar();
})();

(() => {
  // Per-item "采购来源" selects submit their own tiny form as soon as she
  // picks a store -- no separate save button needed.
  document.querySelectorAll(".pd-store-select").forEach((select) => {
    select.addEventListener("change", () => select.form?.requestSubmit());
  });

  const bulkForm = document.getElementById("planStoreBulkForm");
  const bulkApply = document.getElementById("bulkStoreApply");
  const bulkCount = document.getElementById("bulkSelectedCount");
  const planCheckboxes = [...document.querySelectorAll(".pd-plan-checkbox")];
  if (!bulkForm || !bulkApply || !planCheckboxes.length) return;

  function updateBulkBar() {
    const checked = planCheckboxes.filter((box) => box.checked).length;
    bulkCount.textContent = String(checked);
    bulkApply.disabled = checked === 0;
  }
  planCheckboxes.forEach((box) => box.addEventListener("change", updateBulkBar));
  updateBulkBar();
})();
