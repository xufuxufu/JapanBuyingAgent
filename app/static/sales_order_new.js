(() => {
  const orderForm = document.getElementById("salesOrderForm");
  const investigationForm = document.getElementById("investigationForm");
  const orderModeRadio = document.getElementById("demandModeOrder");
  const investigationModeRadio = document.getElementById("demandModeInvestigation");
  function applyDemandMode() {
    const isInvestigation = investigationModeRadio?.checked;
    if (orderForm) orderForm.hidden = Boolean(isInvestigation);
    if (investigationForm) investigationForm.hidden = !isInvestigation;
  }
  orderModeRadio?.addEventListener("change", applyDemandMode);
  investigationModeRadio?.addEventListener("change", applyDemandMode);
})();

(() => {
  const form = document.getElementById("salesOrderForm");
  if (!form) return;
  const isEdit = Boolean(window.__salesOrderEdit);
  const editData = window.__salesOrderEditData || null;

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

  function inventoryText(quantity) {
    // Unknown (no snapshot data for this warehouse) must never look like a
    // known zero -- show "--" rather than silently omitting the region.
    return quantity === null || quantity === undefined ? "--" : String(quantity);
  }

  function renderResultRow(container, { imageUrl, showThumb = true, label, sublabels }, onPick) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = showThumb ? "row search-result-item" : "row search-result-item search-result-item-no-thumb";
    if (showThumb) {
      const thumbWrap = document.createElement("span");
      thumbWrap.className = "shipment-pick-thumb-slot";
      if (imageUrl) {
        const img = document.createElement("img");
        img.className = "search-result-thumb zoomable";
        img.src = imageUrl;
        img.loading = "lazy";
        img.referrerPolicy = "no-referrer";
        img.dataset.zoomSrc = imageUrl;
        thumbWrap.appendChild(img);
        const hint = document.createElement("span");
        hint.className = "zoom-hint";
        hint.textContent = "+";
        thumbWrap.appendChild(hint);
      } else {
        const placeholder = document.createElement("span");
        placeholder.className = "search-result-thumb search-result-thumb-empty";
        placeholder.textContent = "无图";
        thumbWrap.appendChild(placeholder);
      }
      row.appendChild(thumbWrap);
    }
    const main = document.createElement("span");
    main.className = "search-result-main";
    const strong = document.createElement("strong");
    strong.textContent = label;
    main.appendChild(strong);
    sublabels.filter(Boolean).forEach((text) => {
      const small = document.createElement("small");
      small.textContent = text;
      main.appendChild(small);
    });
    row.appendChild(main);
    row.addEventListener("click", () => onPick());
    container.appendChild(row);
  }

  // ---- Address paste-parser wiring (shared parseAddressPasteText from address_paste.js) ----
  function wirePasteParser({ buttonId, textareaId, errorId, onParsed }) {
    const button = document.getElementById(buttonId);
    if (!button) return;
    button.addEventListener("click", () => {
      const errorEl = document.getElementById(errorId);
      errorEl.hidden = true;
      const result = parseAddressPasteText(document.getElementById(textareaId).value);
      if (!result) {
        errorEl.textContent = "未能识别手机号，请手动填写（未修改任何字段）";
        errorEl.hidden = false;
        return;
      }
      onParsed(result);
    });
  }

  // ---- Customer picker ----
  const customerIdInput = document.getElementById("customerIdInput");
  const customerAddressIdInput = document.getElementById("customerAddressIdInput");
  const customerPicked = document.getElementById("customerPicked");
  const customerPickedName = document.getElementById("customerPickedName");
  const customerPickedMeta = document.getElementById("customerPickedMeta");
  const customerAddressPicker = document.getElementById("customerAddressPicker");
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

  wirePasteParser({
    buttonId: "newCustomerPasteParse", textareaId: "newCustomerPasteText", errorId: "newCustomerPasteError",
    onParsed: (result) => {
      if (result.name) document.getElementById("newCustomerRecipientName").value = result.name;
      document.getElementById("newCustomerRecipientPhone").value = result.phone;
      if (result.address) document.getElementById("newCustomerAddress").value = result.address;
    },
  });
  wirePasteParser({
    buttonId: "recipientPasteParse", textareaId: "recipientPasteText", errorId: "recipientPasteError",
    onParsed: (result) => {
      if (result.name) recipientNameInput.value = result.name;
      recipientPhoneInput.value = result.phone;
      if (result.address) shippingAddressInput.value = result.address;
    },
  });

  function applyAddressToRecipientFields(address, customerName) {
    recipientNameInput.value = address ? address.recipient_name : (customerName || "");
    recipientPhoneInput.value = address ? (address.phone || "") : "";
    shippingAddressInput.value = address ? address.address : "";
    customerAddressIdInput.value = address ? String(address.id) : "";
  }

  function buildNewAddressInlineForm(customerId, customerName, onSaved) {
    const wrap = document.createElement("div");
    wrap.className = "stack";
    wrap.innerHTML = `
      <label>收件人姓名<input class="inline-new-address-name" maxlength="255"></label>
      <label>收件手机号<input class="inline-new-address-phone" maxlength="50" inputmode="tel"></label>
      <label>收货地址<input class="inline-new-address-address" maxlength="500"></label>
      <div class="button-row"><button type="button" class="compact inline-new-address-save">保存新地址</button></div>
      <p class="error inline-new-address-error" hidden></p>
    `;
    wrap.querySelector(".inline-new-address-save").addEventListener("click", async function save(allowDuplicate) {
      const errorEl = wrap.querySelector(".inline-new-address-error");
      errorEl.hidden = true;
      const recipient_name = wrap.querySelector(".inline-new-address-name").value.trim();
      const address = wrap.querySelector(".inline-new-address-address").value.trim();
      if (!recipient_name || !address) {
        errorEl.textContent = "收件人姓名和收货地址不能为空";
        errorEl.hidden = false;
        return;
      }
      const body = {
        recipient_name, address, phone: wrap.querySelector(".inline-new-address-phone").value.trim() || null,
        allow_duplicate: allowDuplicate === true,
      };
      const response = await fetch(`/api/customers/${customerId}/addresses`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (response.ok) {
        const created = await response.json();
        onSaved(created);
        return;
      }
      if (response.status === 409) {
        if (window.confirm("该客户已存在相同收货地址，是否仍然保存为新地址？")) {
          const retryBody = { ...body, allow_duplicate: true };
          const retry = await fetch(`/api/customers/${customerId}/addresses`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(retryBody),
          });
          if (retry.ok) onSaved(await retry.json());
        }
        return;
      }
      errorEl.textContent = "保存失败";
      errorEl.hidden = false;
    });
    return wrap;
  }

  async function renderAddressPicker(customerId, customerName, { autoApply = true } = {}) {
    customerAddressPicker.textContent = "";
    let addresses = [];
    try {
      addresses = await fetchJson(`/api/customers/${customerId}/addresses`);
    } catch (err) {
      return;
    }
    if (!addresses.length && autoApply) {
      applyAddressToRecipientFields(null, customerName);
    }
    const label = document.createElement("label");
    label.textContent = "收货地址";
    const select = document.createElement("select");
    if (!autoApply) {
      const keepOption = document.createElement("option");
      keepOption.value = "";
      keepOption.textContent = "保持当前收货信息（不更改）";
      select.appendChild(keepOption);
    }
    addresses.forEach((address) => {
      const option = document.createElement("option");
      option.value = String(address.id);
      const parts = [address.label, address.recipient_name, address.phone, address.address].filter(Boolean);
      option.textContent = parts.join(" / ");
      select.appendChild(option);
    });
    const newOption = document.createElement("option");
    newOption.value = "__new__";
    newOption.textContent = "+ 新增地址";
    select.appendChild(newOption);

    const inlineFormSlot = document.createElement("div");

    select.addEventListener("change", () => {
      inlineFormSlot.textContent = "";
      if (select.value === "__new__") {
        const inlineForm = buildNewAddressInlineForm(customerId, customerName, (created) => {
          renderAddressPicker(customerId, customerName, { autoApply: true });
        });
        inlineFormSlot.appendChild(inlineForm);
        return;
      }
      const picked = addresses.find((address) => String(address.id) === select.value);
      applyAddressToRecipientFields(picked || null, customerName);
    });
    label.appendChild(select);
    customerAddressPicker.appendChild(label);
    customerAddressPicker.appendChild(inlineFormSlot);

    if (addresses.length) {
      const defaultAddress = addresses.find((address) => address.is_default) || addresses[0];
      if (autoApply) {
        select.value = String(defaultAddress.id);
        applyAddressToRecipientFields(defaultAddress, customerName);
      } else {
        select.value = "";
      }
    }
  }

  function selectCustomer(customer, { autoApply = true } = {}) {
    customerIdInput.value = String(customer.id);
    customerPickedName.textContent = customer.name;
    const meta = [customer.phone, customer.wechat_name].filter(Boolean).join(" · ");
    customerPickedMeta.textContent = meta || "暂无电话/微信名";
    customerPicked.hidden = false;
    customerSearchBlock.hidden = true;
    customerSearchResults.hidden = true;
    customerSearchInput.value = "";
    recipientBlock.hidden = false;
    renderAddressPicker(customer.id, customer.name, { autoApply });
  }

  customerChange.addEventListener("click", () => {
    customerIdInput.value = "";
    customerAddressIdInput.value = "";
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
        // Customers have no image at all -- no thumbnail slot, not even a
        // "no image" placeholder, so the row stays compact.
        renderResultRow(customerSearchResults, { showThumb: false, label: customer.name, sublabels: [sub] }, () => selectCustomer(customer));
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
      recipient_name: document.getElementById("newCustomerRecipientName").value.trim() || null,
      recipient_phone: document.getElementById("newCustomerRecipientPhone").value.trim() || null,
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
    row.querySelector(".item-line-amount").textContent = amount.toLocaleString("zh-CN");
    return amount;
  }

  function recalcTotal() {
    let total = 0;
    itemRows.querySelectorAll(".item-card").forEach((row) => {
      total += recalcRow(row);
    });
    orderTotalAmount.textContent = total.toLocaleString("zh-CN");
    itemsEmptyHint.hidden = itemRows.children.length > 0;
  }

  function renumberRows() {
    itemRows.querySelectorAll(".item-card").forEach((row, index) => {
      row.querySelector(".item-index").textContent = String(index + 1);
    });
  }

  // New rows are inserted at the TOP so a phone user adding several items in a
  // row never has to scroll back down to find the "add" button again; the
  // whole visible list is then renumbered 1..N top-to-bottom (a display-only
  // index -- it never touches the underlying saved SalesOrderItem id).
  function addItemRow({ focusSearch = true } = {}) {
    itemSeq += 1;
    const clientId = `c${itemSeq}`;
    const fragment = itemRowTemplate.content.cloneNode(true);
    const row = fragment.querySelector(".item-card");
    row.dataset.clientId = clientId;
    row.dataset.productId = "";
    row.dataset.mode = "existing";

    const searchInput = row.querySelector(".item-product-search");
    const searchResults = row.querySelector(".item-search-results");
    const selectedCard = row.querySelector(".item-selected-card");
    const selectedImage = row.querySelector(".item-selected-image");
    const selectedName = row.querySelector(".item-selected-name");
    const selectedHintPrice = row.querySelector(".item-selected-hint-price");
    const manualToggle = row.querySelector(".item-manual-toggle");
    const manualLabel = row.querySelector(".item-mode-manual");
    const manualInput = row.querySelector(".item-manual-name");
    const manualImageInput = row.querySelector(".item-manual-image");
    const manualImagePreview = row.querySelector(".item-manual-image-preview");
    const existingLabel = row.querySelector(".item-mode-existing");
    const janInput = row.querySelector(".item-jan");
    const priceInput = row.querySelector(".item-price");
    const quantityInput = row.querySelector(".item-quantity");
    const imageSearchMount = row.querySelector(".item-image-search-mount");
    if (imageSearchMount && window.ImageProductSearch) {
      window.ImageProductSearch.mount(imageSearchMount, (product) => pickProduct(product));
    }

    manualImageInput.name = `item_image_${clientId}`;
    manualImageInput.addEventListener("change", () => {
      const file = manualImageInput.files[0];
      if (!file) {
        manualImagePreview.hidden = true;
        return;
      }
      const url = URL.createObjectURL(file);
      manualImagePreview.src = url;
      manualImagePreview.dataset.zoomSrc = url;
      manualImagePreview.hidden = false;
    });

    function pickProduct(product) {
      row.dataset.productId = String(product.id);
      row.dataset.displayName = product.display_name;
      selectedCard.hidden = false;
      if (product.image_url) {
        selectedImage.src = product.image_url;
        selectedImage.dataset.zoomSrc = product.image_url;
        selectedImage.hidden = false;
      } else {
        selectedImage.hidden = true;
      }
      selectedName.textContent = `已选择：${product.display_name}${product.jan ? " · " + product.jan : ""}`;
      if (product.last_sale_price !== null && product.last_sale_price !== undefined) {
        selectedHintPrice.hidden = false;
        selectedHintPrice.textContent = `上次微信售价：${product.last_sale_price} 元（仅供参考，不会自动填入）`;
      } else {
        selectedHintPrice.hidden = true;
      }
      searchInput.value = "";
      searchResults.hidden = true;
      searchResults.textContent = "";
      janInput.value = product.jan || "";
      janInput.readOnly = true;
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
          const inventoryLine = `中国 ${inventoryText(product.china_quantity)} · 日本 ${inventoryText(product.japan_quantity)}`
            + (product.last_sale_price !== null && product.last_sale_price !== undefined ? ` · 上次微信售价 ${product.last_sale_price} 元` : " · 上次微信售价 --");
          const sub = [
            [product.jan, product.qinsi_product_code].filter(Boolean).map((v, i) => (i === 0 ? `JAN：${v}` : `秦丝货号：${v}`)).join(" · "),
            inventoryLine,
          ];
          renderResultRow(searchResults, { imageUrl: product.image_url, label: product.display_name, sublabels: sub }, () => pickProduct(product));
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
      selectedCard.hidden = true;
      manualLabel.hidden = !toManual;
      manualToggle.textContent = toManual ? "改为搜索已有商品" : "该商品未收录，改为手工输入名称/上传图片";
      janInput.readOnly = false;
      if (!toManual) {
        manualInput.value = "";
        manualImageInput.value = "";
        manualImagePreview.hidden = true;
      }
    });

    quantityInput.addEventListener("input", recalcTotal);
    priceInput.addEventListener("input", recalcTotal);

    row.querySelector(".item-remove").addEventListener("click", () => {
      row.remove();
      renumberRows();
      recalcTotal();
    });

    itemRows.insertBefore(row, itemRows.firstChild);
    renumberRows();
    recalcTotal();
    if (focusSearch) searchInput.focus();
    return row;
  }

  document.getElementById("addItemRow").addEventListener("click", () => addItemRow());

  // ---- Edit-mode prefill ----
  if (isEdit && editData) {
    selectCustomer(editData.customer, { autoApply: false });
    // Existing saved order-line order is a historical fact; prefilled rows are
    // appended in original order (not reversed) even though new manual
    // additions during this edit session still land on top.
    [...editData.items].reverse().forEach((item) => {
      const row = addItemRow({ focusSearch: false });
      if (item.product_id) {
        row.dataset.productId = String(item.product_id);
        row.dataset.displayName = item.display_name;
        row.dataset.mode = "existing";
        const selectedCard = row.querySelector(".item-selected-card");
        const selectedImage = row.querySelector(".item-selected-image");
        const selectedName = row.querySelector(".item-selected-name");
        selectedCard.hidden = false;
        if (item.image_url) {
          selectedImage.src = item.image_url;
          selectedImage.dataset.zoomSrc = item.image_url;
          selectedImage.hidden = false;
        }
        selectedName.textContent = `已选择：${item.display_name}`;
        row.querySelector(".item-jan").value = item.jan || "";
        row.querySelector(".item-jan").readOnly = true;
      } else {
        row.dataset.mode = "manual";
        row.querySelector(".item-mode-existing").hidden = true;
        row.querySelector(".item-mode-manual").hidden = false;
        row.querySelector(".item-manual-toggle").textContent = "改为搜索已有商品";
        row.querySelector(".item-manual-name").value = item.manual_name || "";
        row.querySelector(".item-jan").value = item.jan || "";
        if (item.image_url) {
          const preview = row.querySelector(".item-manual-image-preview");
          preview.src = item.image_url;
          preview.dataset.zoomSrc = item.image_url;
          preview.hidden = false;
        }
      }
      row.querySelector(".item-quantity").value = item.quantity;
      row.querySelector(".item-price").value = item.unit_sale_price;
      row.querySelector(".item-note").value = item.note || "";
    });
    renumberRows();
    recalcTotal();
  } else {
    addItemRow();
  }

  // ---- Validate + build the submit payload (shared by the confirm modal) ----
  const formError = document.getElementById("formError");

  function validateAndCollectItems() {
    formError.hidden = true;
    formError.textContent = "";
    if (!customerIdInput.value) {
      formError.textContent = "请先选择或新增客户";
      formError.hidden = false;
      return null;
    }
    const rows = [...itemRows.querySelectorAll(".item-card")];
    if (!rows.length) {
      formError.textContent = "请至少添加一个商品";
      formError.hidden = false;
      return null;
    }
    const items = [];
    for (const row of rows) {
      const mode = row.dataset.mode;
      const productId = row.dataset.productId;
      const manualName = row.querySelector(".item-manual-name").value.trim();
      const hasManualImage = Boolean(row.querySelector(".item-manual-image").files[0]) || (isEdit && !row.querySelector(".item-manual-image-preview").hidden && mode === "manual");
      const quantity = Number(row.querySelector(".item-quantity").value);
      const price = row.querySelector(".item-price").value;
      if (mode === "existing" && !productId) {
        formError.textContent = "请为每个商品选择已有商品，或切换为手工输入名称/图片";
        formError.hidden = false;
        return null;
      }
      if (mode === "manual" && !manualName && !hasManualImage) {
        formError.textContent = "手工商品必须填写商品名或上传图片";
        formError.hidden = false;
        return null;
      }
      if (!Number.isFinite(quantity) || quantity <= 0) {
        formError.textContent = "商品数量必须大于0";
        formError.hidden = false;
        return null;
      }
      if (price === "" || Number(price) < 0) {
        formError.textContent = "微信售价不能为空或负数";
        formError.hidden = false;
        return null;
      }
      items.push({
        client_id: row.dataset.clientId,
        product_id: mode === "existing" ? Number(productId) : null,
        manual_name: mode === "manual" ? (manualName || null) : null,
        jan: row.querySelector(".item-jan").value.trim() || null,
        quantity,
        unit_sale_price: price,
        note: row.querySelector(".item-note").value.trim() || null,
        display_name: mode === "existing" ? row.dataset.displayName : (manualName || "手工商品"),
      });
    }
    return items;
  }

  // ---- Submit confirmation modal ----
  const confirmModalOverlay = document.getElementById("confirmModalOverlay");
  const submitOrderButton = document.getElementById("submitOrderButton");

  submitOrderButton.addEventListener("click", () => {
    const items = validateAndCollectItems();
    if (!items) return;

    document.getElementById("confirmCustomerName").textContent = customerPickedName.textContent || "—";
    document.getElementById("confirmRecipientName").textContent = recipientNameInput.value || "—";
    document.getElementById("confirmRecipientPhone").textContent = recipientPhoneInput.value || "";
    document.getElementById("confirmShippingAddress").textContent = shippingAddressInput.value || "—";

    const listEl = document.getElementById("confirmItemsList");
    listEl.textContent = "";
    let totalQuantity = 0;
    let totalAmount = 0;
    items.forEach((item) => {
      const amount = item.quantity * Number(item.unit_sale_price || 0);
      totalQuantity += item.quantity;
      totalAmount += amount;
      const row = document.createElement("div");
      row.className = "confirm-modal-item";
      row.innerHTML = `<span>${item.display_name}<small>数量 ${item.quantity} × ¥${item.unit_sale_price}</small></span><span>¥${amount.toFixed(2)}</span>`;
      listEl.appendChild(row);
    });
    document.getElementById("confirmTotalQuantity").textContent = String(totalQuantity);
    document.getElementById("confirmTotalAmount").textContent = totalAmount.toFixed(2);

    document.getElementById("itemsJsonInput").value = JSON.stringify(items);
    confirmModalOverlay.hidden = false;
  });

  document.getElementById("confirmModalBack").addEventListener("click", () => {
    confirmModalOverlay.hidden = true;
  });
  document.getElementById("confirmModalSubmit").addEventListener("click", () => {
    submitOrderButton.disabled = true;
    form.submit();
  });
})();
