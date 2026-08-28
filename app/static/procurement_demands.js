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
