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

(() => {
  // ---- Plan editing (planned_quantity / note / product association) before or after purchasing starts ----
  function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  document.querySelectorAll(".plan-edit-toggle").forEach((toggle) => {
    const form = toggle.nextElementSibling;
    toggle.addEventListener("click", () => {
      form.hidden = !form.hidden;
    });
    form.querySelector(".plan-edit-cancel")?.addEventListener("click", () => {
      form.hidden = true;
    });
    const quantityInput = form.querySelector(".plan-edit-quantity");
    quantityInput?.addEventListener("change", () => {
      const min = Number(quantityInput.min) || 1;
      if (Number(quantityInput.value) < min) quantityInput.value = min;
    });

    const searchInput = form.querySelector(".plan-edit-product-search");
    if (!searchInput) return;
    const results = form.querySelector(".plan-edit-product-results");
    const productIdInput = form.querySelector(".plan-edit-product-id");
    const picked = form.querySelector(".plan-edit-product-picked");
    const runSearch = debounce(async (value) => {
      if (!value.trim()) {
        results.hidden = true;
        results.textContent = "";
        return;
      }
      let products = [];
      try {
        const response = await fetch(`/api/procurement/products/search?q=${encodeURIComponent(value)}`, { headers: { Accept: "application/json" } });
        products = await response.json();
      } catch (err) {
        return;
      }
      results.textContent = "";
      products.forEach((product) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "row search-result-item search-result-item-no-thumb";
        const main = document.createElement("span");
        const strong = document.createElement("strong");
        strong.textContent = product.display_name;
        main.appendChild(strong);
        const small = document.createElement("small");
        small.textContent = [product.jan, product.qinsi_product_code].filter(Boolean).join(" · ");
        main.appendChild(small);
        row.appendChild(main);
        row.addEventListener("click", () => {
          productIdInput.value = String(product.id);
          picked.hidden = false;
          picked.textContent = `将关联为：${product.display_name}`;
          searchInput.value = "";
          results.hidden = true;
        });
        results.appendChild(row);
      });
      results.hidden = products.length === 0;
    }, 250);
    searchInput.addEventListener("input", (event) => runSearch(event.target.value));
  });
})();
