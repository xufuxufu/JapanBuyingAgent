(() => {
  const form = document.getElementById("shortageForm");
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

  function inventoryText(quantity) {
    // Unknown (no snapshot data for that warehouse) must never look like a
    // known zero -- show "--" rather than hiding the field.
    return quantity === null || quantity === undefined ? "--" : String(quantity);
  }

  const productIdInput = document.getElementById("shortageProductIdInput");
  const picked = document.getElementById("shortagePicked");
  const pickedThumbSlot = document.getElementById("shortagePickedThumbSlot");
  const pickedImage = document.getElementById("shortagePickedImage");
  const pickedName = document.getElementById("shortagePickedName");
  const pickedMeta = document.getElementById("shortagePickedMeta");
  const searchBlock = document.getElementById("shortageSearchBlock");
  const searchInput = document.getElementById("shortageSearchInput");
  const searchResults = document.getElementById("shortageSearchResults");
  const manualToggle = document.getElementById("shortageManualToggle");
  const manualBlock = document.getElementById("shortageManualBlock");
  const manualInput = document.getElementById("shortageManualInput");
  const manualImageInput = document.getElementById("shortageManualImageInput");
  const manualImagePreview = document.getElementById("shortageManualImagePreview");
  const changeButton = document.getElementById("shortageChange");
  const formError = document.getElementById("shortageFormError");
  const imageSearchMount = document.getElementById("shortageImageSearchMount");

  function pickProduct(product) {
    productIdInput.value = String(product.id);
    manualInput.value = "";
    manualImageInput.value = "";
    manualImagePreview.hidden = true;
    manualBlock.hidden = true;
    if (product.image_url) {
      pickedImage.src = product.image_url;
      pickedImage.dataset.zoomSrc = product.image_url;
      pickedImage.hidden = false;
      pickedThumbSlot.classList.remove("image-load-failed");
    } else {
      pickedImage.hidden = true;
      pickedThumbSlot.classList.add("image-load-failed");
    }
    pickedName.textContent = product.display_name;
    const metaBits = [
      product.jan ? `JAN：${product.jan}` : null,
      product.qinsi_product_code ? `秦丝货号：${product.qinsi_product_code}` : null,
      `中国 ${inventoryText(product.china_quantity)} · 日本 ${inventoryText(product.japan_quantity)}`,
      `采购中 ${product.in_transit_quantity ?? 0}`,
      `7天 ${inventoryText(product.sales_7d)} · 30天 ${inventoryText(product.sales_30d)}`,
      "参考订货量 --",
    ];
    pickedMeta.textContent = metaBits.filter(Boolean).join(" · ");
    picked.hidden = false;
    searchBlock.hidden = true;
  }

  if (imageSearchMount && window.ImageProductSearch) {
    window.ImageProductSearch.mount(imageSearchMount, pickProduct);
  }

  changeButton.addEventListener("click", () => {
    productIdInput.value = "";
    picked.hidden = true;
    searchBlock.hidden = false;
    searchInput.value = "";
  });

  function renderResultRow(product) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "row search-result-item";
    const thumbWrap = document.createElement("span");
    thumbWrap.className = "shipment-pick-thumb-slot";
    if (product.image_url) {
      const img = document.createElement("img");
      img.className = "search-result-thumb zoomable";
      img.src = product.image_url;
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.dataset.zoomSrc = product.image_url;
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
    const main = document.createElement("span");
    main.className = "search-result-main";
    const strong = document.createElement("strong");
    strong.textContent = product.display_name;
    main.appendChild(strong);
    const lines = [
      [product.jan, product.qinsi_product_code].filter(Boolean).join(" · "),
      `中国 ${inventoryText(product.china_quantity)} · 日本 ${inventoryText(product.japan_quantity)} · 采购中 ${product.in_transit_quantity ?? 0}`,
      `7天 ${inventoryText(product.sales_7d)} · 30天 ${inventoryText(product.sales_30d)} · 参考订货量 --`,
    ];
    lines.filter(Boolean).forEach((text) => {
      const small = document.createElement("small");
      small.textContent = text;
      main.appendChild(small);
    });
    row.appendChild(main);
    // A tap on the thumbnail zooms (handled by the shared image_zoom.js
    // capture-phase listener, which stops this click before it reaches here);
    // anywhere else on the card picks the product.
    row.addEventListener("click", () => pickProduct(product));
    return row;
  }

  const runSearch = debounce(async (value) => {
    if (!value.trim()) {
      searchResults.hidden = true;
      searchResults.textContent = "";
      return;
    }
    let results = [];
    try {
      results = await fetchJson(`/api/procurement/products/search?q=${encodeURIComponent(value)}`);
    } catch (err) {
      return;
    }
    searchResults.textContent = "";
    if (!results.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "没有匹配商品，可点击下方“手工输入名称/上传图片”。";
      searchResults.appendChild(empty);
    } else {
      results.forEach((product) => searchResults.appendChild(renderResultRow(product)));
    }
    searchResults.hidden = false;
  }, 250);

  searchInput.addEventListener("input", (event) => runSearch(event.target.value));

  manualToggle.addEventListener("click", () => {
    manualBlock.hidden = !manualBlock.hidden;
    if (!manualBlock.hidden) {
      productIdInput.value = "";
      picked.hidden = true;
    }
  });

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

  // ---- Quantity stepper ----
  const quantityInput = document.getElementById("shortageQuantityInput");
  document.querySelectorAll(".quantity-step").forEach((button) => {
    button.addEventListener("click", () => {
      const step = Number(button.dataset.step);
      const next = Math.max(1, (Number(quantityInput.value) || 1) + step);
      quantityInput.value = next;
    });
  });
  quantityInput.addEventListener("change", () => {
    if (!Number.isFinite(Number(quantityInput.value)) || Number(quantityInput.value) < 1) {
      quantityInput.value = 1;
    }
  });

  form.addEventListener("submit", (event) => {
    formError.hidden = true;
    const hasManualImage = Boolean(manualImageInput.files[0]);
    if (!productIdInput.value && !manualInput.value.trim() && !hasManualImage) {
      event.preventDefault();
      formError.textContent = "请搜索选择商品，或手工输入商品名称/上传图片";
      formError.hidden = false;
      return;
    }
    let hiddenManual = form.querySelector('input[name="manual_name"]');
    if (!hiddenManual) {
      hiddenManual = document.createElement("input");
      hiddenManual.type = "hidden";
      hiddenManual.name = "manual_name";
      form.appendChild(hiddenManual);
    }
    hiddenManual.value = manualInput.value.trim();
    document.getElementById("shortageSubmitButton").disabled = true;
  });
})();
