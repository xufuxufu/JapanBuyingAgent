// Shared "拍照搜商品" widget. Mount once per page slot:
//   ImageProductSearch.mount(containerEl, (product) => { ...call the page's
//   own existing pickProduct-style selection function... });
//
// The callback receives an object shaped like:
//   { id, display_name, image_url, jan, qinsi_product_code,
//     china_quantity, japan_quantity, in_transit_quantity: 0 }
// so pages can pass it straight into whatever their text-search result
// already builds -- this widget never fills in a form itself.
(() => {
  function inventoryText(known, value) {
    if (!known) return "--";
    return String(value ?? 0);
  }

  function mount(container, onSelect) {
    container.innerHTML = `
      <div class="image-search-triggers">
        <button type="button" class="secondary compact image-search-camera-btn">拍照搜索</button>
        <button type="button" class="secondary compact image-search-gallery-btn">从相册选择</button>
      </div>
      <input type="file" accept="image/*" capture="environment" class="image-search-camera-input" hidden>
      <input type="file" accept="image/*" class="image-search-gallery-input" hidden>
      <div class="image-search-panel" hidden>
        <div class="image-search-panel-header">
          <img class="image-search-query-preview" alt="查询图片">
          <span class="image-search-status">搜索中…</span>
          <button type="button" class="secondary compact image-search-close-btn">取消</button>
        </div>
        <p class="image-search-hint muted">找到以下相似商品，请选择</p>
        <div class="image-search-results"></div>
      </div>
    `;
    const cameraBtn = container.querySelector(".image-search-camera-btn");
    const galleryBtn = container.querySelector(".image-search-gallery-btn");
    const cameraInput = container.querySelector(".image-search-camera-input");
    const galleryInput = container.querySelector(".image-search-gallery-input");
    const panel = container.querySelector(".image-search-panel");
    const preview = container.querySelector(".image-search-query-preview");
    const status = container.querySelector(".image-search-status");
    const hint = container.querySelector(".image-search-hint");
    const results = container.querySelector(".image-search-results");
    const closeBtn = container.querySelector(".image-search-close-btn");

    cameraBtn.addEventListener("click", () => cameraInput.click());
    galleryBtn.addEventListener("click", () => galleryInput.click());
    closeBtn.addEventListener("click", () => {
      panel.hidden = true;
    });

    async function runSearch(file) {
      if (!file) return;
      const objectUrl = URL.createObjectURL(file);
      preview.src = objectUrl;
      panel.hidden = false;
      status.hidden = false;
      status.textContent = "搜索中…";
      hint.hidden = true;
      results.hidden = true;
      results.textContent = "";

      const formData = new FormData();
      formData.append("image", file);
      formData.append("top_k", "10");

      let payload;
      try {
        const response = await fetch("/api/products/image-search", { method: "POST", body: formData });
        payload = await response.json();
      } catch (err) {
        status.textContent = "图片搜索暂时不可用，请稍后重试。";
        return;
      }

      if (payload.status === "no_index") {
        status.textContent = payload.message || "图片搜索索引尚未建立，请先由管理员重建索引。";
        return;
      }
      if (payload.status === "error") {
        status.textContent = payload.message || "图片搜索暂时不可用，请稍后重试。";
        return;
      }
      const items = payload.results || [];
      status.hidden = true;
      if (!items.length) {
        hint.hidden = false;
        hint.textContent = "没有找到相似商品，请人工确认或改用文字搜索。";
        return;
      }
      hint.hidden = false;
      hint.textContent = "找到以下相似商品，请选择";
      const lowSimilarity = items.every((item) => item.similarity_score < 0.5);
      if (lowSimilarity) {
        hint.textContent += "（结果相似度较低，请人工确认）";
      }
      results.hidden = false;
      items.forEach((item) => {
        const card = document.createElement("article");
        card.className = "image-search-result-card";
        const thumbSlot = document.createElement("div");
        thumbSlot.className = "product-thumb-slot";
        const empty = document.createElement("span");
        empty.className = "product-image-empty";
        empty.textContent = "无图";
        thumbSlot.appendChild(empty);
        if (item.image_url) {
          const img = document.createElement("img");
          img.className = "product-thumb zoomable";
          img.src = item.image_url;
          img.dataset.zoomSrc = item.image_url;
          img.alt = item.name || "商品图片";
          img.loading = "lazy";
          img.referrerPolicy = "no-referrer";
          img.addEventListener("error", () => thumbSlot.classList.add("image-load-failed"));
          thumbSlot.appendChild(img);
          const zoomHint = document.createElement("span");
          zoomHint.className = "zoom-hint";
          zoomHint.textContent = "点击查看大图";
          thumbSlot.appendChild(zoomHint);
        } else {
          thumbSlot.classList.add("image-load-failed");
        }
        const info = document.createElement("div");
        info.className = "image-search-result-info";
        const name = document.createElement("h4");
        name.className = "image-search-result-name";
        name.textContent = item.name || "未命名商品";
        const meta = document.createElement("p");
        meta.className = "muted image-search-result-meta";
        meta.textContent = [
          item.jan ? `JAN：${item.jan}` : null,
          `中国 ${inventoryText(item.china_known, item.china_quantity)} · 日本 ${inventoryText(item.japan_known, item.japan_quantity)}`,
          `相似度 ${(item.similarity_score * 100).toFixed(1)}%`,
        ].filter(Boolean).join(" · ");
        info.appendChild(name);
        info.appendChild(meta);
        card.appendChild(thumbSlot);
        card.appendChild(info);
        card.addEventListener("click", () => {
          onSelect({
            id: item.product_id,
            display_name: item.name,
            image_url: item.image_url,
            jan: item.jan,
            qinsi_product_code: item.qinsi_product_code,
            china_quantity: item.china_quantity,
            japan_quantity: item.japan_quantity,
            in_transit_quantity: 0,
          });
          panel.hidden = true;
        });
        results.appendChild(card);
      });
    }

    cameraInput.addEventListener("change", () => runSearch(cameraInput.files[0]));
    galleryInput.addEventListener("change", () => runSearch(galleryInput.files[0]));
  }

  window.ImageProductSearch = { mount };
})();
