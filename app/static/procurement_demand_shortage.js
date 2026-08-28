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

  const productIdInput = document.getElementById("shortageProductIdInput");
  const picked = document.getElementById("shortagePicked");
  const pickedName = document.getElementById("shortagePickedName");
  const searchBlock = document.getElementById("shortageSearchBlock");
  const searchInput = document.getElementById("shortageSearchInput");
  const searchResults = document.getElementById("shortageSearchResults");
  const manualToggle = document.getElementById("shortageManualToggle");
  const manualLabel = document.getElementById("shortageManualLabel");
  const manualInput = document.getElementById("shortageManualInput");
  const changeButton = document.getElementById("shortageChange");
  const formError = document.getElementById("shortageFormError");

  function pickProduct(product) {
    productIdInput.value = String(product.id);
    manualInput.value = "";
    pickedName.textContent = `${product.display_name}${product.jan ? " · " + product.jan : ""}`;
    picked.hidden = false;
    searchBlock.hidden = true;
  }

  changeButton.addEventListener("click", () => {
    productIdInput.value = "";
    picked.hidden = true;
    searchBlock.hidden = false;
    searchInput.value = "";
  });

  const runSearch = debounce(async (value) => {
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
        const row = document.createElement("button");
        row.type = "button";
        row.className = "row search-result-item";
        const main = document.createElement("span");
        const strong = document.createElement("strong");
        strong.textContent = product.display_name;
        main.appendChild(strong);
        const sub = [product.jan, product.internal_sku].filter(Boolean).join(" · ");
        if (sub) {
          const small = document.createElement("small");
          small.textContent = sub;
          main.appendChild(small);
        }
        row.appendChild(main);
        row.addEventListener("click", () => pickProduct(product));
        searchResults.appendChild(row);
      });
    }
    searchResults.hidden = false;
  }, 250);

  searchInput.addEventListener("input", (event) => runSearch(event.target.value));

  manualToggle.addEventListener("click", () => {
    manualLabel.hidden = !manualLabel.hidden;
    if (!manualLabel.hidden) {
      productIdInput.value = "";
      picked.hidden = true;
    }
  });

  form.addEventListener("submit", (event) => {
    formError.hidden = true;
    if (!productIdInput.value && !manualInput.value.trim()) {
      event.preventDefault();
      formError.textContent = "请搜索选择商品，或手工输入商品名称";
      formError.hidden = false;
      return;
    }
    // Manual name travels as its own form field so the server can fall back to it
    // when no product was picked from search.
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
