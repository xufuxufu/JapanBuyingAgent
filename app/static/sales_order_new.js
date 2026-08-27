(() => {
  const form = document.getElementById("salesOrderForm");
  if (!form) return;

  function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  async function fetchJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`request failed: ${response.status}`);
    return response.json();
  }

  function renderResultRow(container, label, sublabel, onPick) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "row search-result-item";
    const main = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = label;
    main.appendChild(strong);
    if (sublabel) {
      const small = document.createElement("small");
      small.textContent = sublabel;
      main.appendChild(small);
    }
    row.appendChild(main);
    row.addEventListener("click", onPick);
    container.appendChild(row);
  }

  // ---- Customer picker ----
  const customerIdInput = document.getElementById("customerIdInput");
  const customerPicked = document.getElementById("customerPicked");
  const customerPickedName = document.getElementById("customerPickedName");
  const customerPickedMeta = document.getElementById("customerPickedMeta");
  const customerSearchBlock = document.getElementById("customerSearchBlock");
  const customerSearchInput = document.getElementById("customerSearchInput");
  const customerSearchResults = document.getElementById("customerSearchResults");
  const customerQuickAddToggle = document.getElementById("customerQuickAddToggle");
  const customerQuickAddForm = document.getElementById("customerQuickAddForm");
  const customerQuickAddSave = document.getElementById("customerQuickAddSave");
  const customerQuickAddCancel = document.getElementById("customerQuickAddCancel");
  const customerQuickAddError = document.getElementById("customerQuickAddError");
  const customerChange = document.getElementById("customerChange");
  const recipientBlock = document.getElementById("recipientBlock");
  const recipientNameInput = document.getElementById("recipientNameInput");
  const recipientPhoneInput = document.getElementById("recipientPhoneInput");
  const shippingAddressInput = document.getElementById("shippingAddressInput");

  function selectCustomer(customer) {
    customerIdInput.value = String(customer.id);
    customerPickedName.textContent = customer.name;
    const meta = [customer.phone, customer.wechat_name].filter(Boolean).join(" · ");
    customerPickedMeta.textContent = meta || "暂无电话/微信名";
    customerPicked.hidden = false;
    customerSearchBlock.hidden = true;
    customerSearchResults.hidden = true;
    customerSearchInput.value = "";
    // Recipient defaults to the customer themselves; the salesperson can still
    // hand-edit it below for a one-off different recipient on this order.
    recipientNameInput.value = customer.name || "";
    recipientPhoneInput.value = customer.phone || "";
    shippingAddressInput.value = customer.address || "";
    recipientBlock.hidden = false;
  }

  customerChange.addEventListener("click", () => {
    customerIdInput.value = "";
    customerPicked.hidden = true;
    customerSearchBlock.hidden = false;
    recipientBlock.hidden = true;
  });

  const runCustomerSearch = debounce(async (value) => {
    if (!value.trim()) {
      customerSearchResults.hidden = true;
      customerSearchResults.textContent = "";
      return;
    }
    let results = [];
    try {
      results = await fetchJson(`/api/customers/search?q=${encodeURIComponent(value)}`);
    } catch (err) {
      return;
    }
    customerSearchResults.textContent = "";
    if (!results.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "没有匹配客户，可使用下方“快速新增客户”。";
      customerSearchResults.appendChild(empty);
    } else {
      results.forEach((customer) => {
        const sub = [customer.phone, customer.wechat_name].filter(Boolean).join(" · ");
        renderResultRow(customerSearchResults, customer.name, sub, () => selectCustomer(customer));
      });
    }
    customerSearchResults.hidden = false;
  }, 250);

  customerSearchInput.addEventListener("input", (event) => runCustomerSearch(event.target.value));

  customerQuickAddToggle.addEventListener("click", () => {
    customerQuickAddForm.hidden = !customerQuickAddForm.hidden;
  });
  customerQuickAddCancel.addEventListener("click", () => {
    customerQuickAddForm.hidden = true;
  });
  customerQuickAddSave.addEventListener("click", async () => {
    const name = document.getElementById("newCustomerName").value.trim();
    customerQuickAddError.hidden = true;
    if (!name) {
      customerQuickAddError.textContent = "客户姓名不能为空";
      customerQuickAddError.hidden = false;
      return;
    }
    const payload = {
      name,
      phone: document.getElementById("newCustomerPhone").value.trim() || null,
      wechat_name: document.getElementById("newCustomerWechat").value.trim() || null,
      address: document.getElementById("newCustomerAddress").value.trim() || null,
      note: document.getElementById("newCustomerNote").value.trim() || null,
    };
    customerQuickAddSave.disabled = true;
    try {
      const response = await fetch("/api/customers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || "新增客户失败");
      }
      const customer = await response.json();
      customerQuickAddForm.hidden = true;
      selectCustomer(customer);
    } catch (err) {
      customerQuickAddError.textContent = err.message || "新增客户失败";
      customerQuickAddError.hidden = false;
    } finally {
      customerQuickAddSave.disabled = false;
    }
  });

  // ---- Salesperson (only relevant once more than one salesperson exists) ----
  document.getElementById("salespersonSelect")?.addEventListener("change", (event) => {
    document.querySelector('input[name="salesperson_id"]').value = event.target.value;
  });

  // ---- Item rows ----
  const itemRows = document.getElementById("itemRows");
  const itemRowTemplate = document.getElementById("itemRowTemplate");
  const itemsEmptyHint = document.getElementById("itemsEmptyHint");
  const orderTotalAmount = document.getElementById("orderTotalAmount");
  let itemSeq = 0;

  function recalcRow(row) {
    const quantity = Number(row.querySelector(".item-quantity").value) || 0;
    const price = Number(row.querySelector(".item-price").value) || 0;
    const amount = quantity * price;
    row.querySelector(".item-line-amount").textContent = amount.toLocaleString("ja-JP");
    return amount;
  }

  function recalcTotal() {
    let total = 0;
    itemRows.querySelectorAll(".item-card").forEach((row) => {
      total += recalcRow(row);
    });
    orderTotalAmount.textContent = total.toLocaleString("ja-JP");
    itemsEmptyHint.hidden = itemRows.children.length > 0;
  }

  function renumberRows() {
    itemRows.querySelectorAll(".item-card").forEach((row, index) => {
      row.querySelector(".item-index").textContent = String(index + 1);
    });
  }

  function addItemRow() {
    itemSeq += 1;
    const fragment = itemRowTemplate.content.cloneNode(true);
    const row = fragment.querySelector(".item-card");
    row.dataset.productId = "";
    row.dataset.mode = "existing";

    const searchInput = row.querySelector(".item-product-search");
    const searchResults = row.querySelector(".item-search-results");
    const selectedName = row.querySelector(".item-selected-name");
    const manualToggle = row.querySelector(".item-manual-toggle");
    const manualLabel = row.querySelector(".item-mode-manual");
    const manualInput = row.querySelector(".item-manual-name");
    const existingLabel = row.querySelector(".item-mode-existing");
    const janInput = row.querySelector(".item-jan");
    const priceInput = row.querySelector(".item-price");
    const quantityInput = row.querySelector(".item-quantity");

    function pickProduct(product) {
      row.dataset.productId = String(product.id);
      selectedName.hidden = false;
      selectedName.textContent = `已选择：${product.display_name}${product.jan ? " · " + product.jan : ""}`;
      searchInput.value = "";
      searchResults.hidden = true;
      searchResults.textContent = "";
      janInput.value = product.jan || "";
      janInput.readOnly = true;
      if (product.sale_price) priceInput.value = product.sale_price;
      recalcTotal();
    }

    const runProductSearch = debounce(async (value) => {
      if (!value.trim()) {
        searchResults.hidden = true;
        searchResults.textContent = "";
        return;
      }
      let results = [];
      try {
        results = await fetchJson(`/api/products/search?q=${encodeURIComponent(value)}`);
      } catch (err) {
        return;
      }
      searchResults.textContent = "";
      if (!results.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "没有匹配商品，可点击下方“手工输入名称”。";
        searchResults.appendChild(empty);
      } else {
        results.forEach((product) => {
          const sub = [product.jan, product.internal_sku].filter(Boolean).join(" · ");
          renderResultRow(searchResults, product.display_name, sub, () => pickProduct(product));
        });
      }
      searchResults.hidden = false;
    }, 250);

    searchInput.addEventListener("input", (event) => runProductSearch(event.target.value));

    manualToggle.addEventListener("click", () => {
      const toManual = row.dataset.mode === "existing";
      row.dataset.mode = toManual ? "manual" : "existing";
      row.dataset.productId = "";
      existingLabel.hidden = toManual;
      searchResults.hidden = true;
      selectedName.hidden = true;
      manualLabel.hidden = !toManual;
      manualToggle.textContent = toManual ? "改为搜索已有商品" : "该商品未收录，改为手工输入名称";
      janInput.readOnly = false;
      if (!toManual) manualInput.value = "";
    });

    quantityInput.addEventListener("input", recalcTotal);
    priceInput.addEventListener("input", recalcTotal);

    row.querySelector(".item-remove").addEventListener("click", () => {
      row.remove();
      renumberRows();
      recalcTotal();
    });

    itemRows.appendChild(row);
    renumberRows();
    recalcTotal();
  }

  document.getElementById("addItemRow").addEventListener("click", addItemRow);
  addItemRow();

  // ---- Submit ----
  const formError = document.getElementById("formError");

  form.addEventListener("submit", (event) => {
    formError.hidden = true;
    formError.textContent = "";
    if (!customerIdInput.value) {
      event.preventDefault();
      formError.textContent = "请先选择或新增客户";
      formError.hidden = false;
      return;
    }
    const rows = [...itemRows.querySelectorAll(".item-card")];
    if (!rows.length) {
      event.preventDefault();
      formError.textContent = "请至少添加一个商品";
      formError.hidden = false;
      return;
    }
    const items = [];
    for (const row of rows) {
      const mode = row.dataset.mode;
      const productId = row.dataset.productId;
      const manualName = row.querySelector(".item-manual-name").value.trim();
      const quantity = Number(row.querySelector(".item-quantity").value);
      const price = row.querySelector(".item-price").value;
      if (mode === "existing" && !productId) {
        event.preventDefault();
        formError.textContent = "请为每个商品选择已有商品，或切换为手工输入名称";
        formError.hidden = false;
        return;
      }
      if (mode === "manual" && !manualName) {
        event.preventDefault();
        formError.textContent = "手工商品名不能为空";
        formError.hidden = false;
        return;
      }
      if (!Number.isFinite(quantity) || quantity <= 0) {
        event.preventDefault();
        formError.textContent = "商品数量必须大于0";
        formError.hidden = false;
        return;
      }
      if (price === "" || Number(price) < 0) {
        event.preventDefault();
        formError.textContent = "实际销售单价不能为空或负数";
        formError.hidden = false;
        return;
      }
      items.push({
        product_id: mode === "existing" ? Number(productId) : null,
        manual_name: mode === "manual" ? manualName : null,
        jan: row.querySelector(".item-jan").value.trim() || null,
        quantity,
        unit_sale_price: price,
        note: row.querySelector(".item-note").value.trim() || null,
      });
    }
    document.getElementById("itemsJsonInput").value = JSON.stringify(items);
    document.getElementById("submitOrderButton").disabled = true;
  });
})();
