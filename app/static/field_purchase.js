(() => {
  "use strict";

  const DB_NAME = "jba-field-purchase";
  const DB_VERSION = 1;
  const IMAGE_CACHE_NAME = "jba-product-images-v1";
  const AUTO_NEXT_KEY = "jba.field.autoNext";
  const AUTO_BATCH_KEY = "jba.field.autoBatchRequest";
  const BUILD_VERSION = "field-iphone-p0-20260726-2";
  const SCAN_INTERVAL_MS = 180;
  const SAME_CODE_DEBOUNCE_MS = 1600;
  const SCAN_SUCCESS_PAUSE_MS = 950;
  const SCAN_TOAST_MS = 1300;
  const ZXING_LOAD_TIMEOUT_MS = 8000;
  const ZXING_STARTUP_WATCHDOG_MS = 3000;
  const API_TIMEOUT_MS = 10000;
  const app = document.querySelector("#fieldPurchaseApp");
  const buildVersion = (() => {
    try {
      const src = document.currentScript?.src || "";
      return new URL(src, location.href).searchParams.get("buildVersion") || BUILD_VERSION;
    } catch (_) {
      return BUILD_VERSION;
    }
  })();

  function requestId(prefix = "req") {
    const value = globalThis.crypto?.randomUUID?.()
      || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${value}`;
  }

  document.querySelectorAll(".js-client-request-id").forEach((input) => {
    if (!input.value) input.value = requestId("batch");
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/service-worker.js").catch(() => {});
  }
  if (!app) return;

  let batchId = Number(app.dataset.batchId) || 0;
  let batchNo = app.dataset.batchNo || "";
  const devMode = app.dataset.devMode === "true";
  if (batchNo) localStorage.setItem("jba.currentBatch", batchNo);

  const video = document.querySelector("#fieldCamera");
  const stage = document.querySelector("#fieldCameraStage");
  const guide = document.querySelector("#fieldScanGuide");
  const placeholder = document.querySelector("#fieldCameraPlaceholder");
  const cameraStatus = document.querySelector("#fieldScannerStatus");
  const debug = document.querySelector("#fieldCameraDebug");
  const copyDiagnosticButton = document.querySelector("#fieldCopyCameraDiagnostic");
  const refreshDiagnosticButton = document.querySelector("#fieldRefreshCameraDiagnostic");
  const startButton = document.querySelector("#fieldStartCamera");
  const stopButton = document.querySelector("#fieldStopCamera");
  const cameraSelect = document.querySelector("#fieldCameraSelect");
  const torchButton = document.querySelector("#fieldTorch");
  const zoomOut = document.querySelector("#fieldZoomOut");
  const zoomReset = document.querySelector("#fieldZoomReset");
  const zoomIn = document.querySelector("#fieldZoomIn");
  const manualForm = document.querySelector("#fieldManualForm");
  const manualSubmit = manualForm?.querySelector("button[type='submit']");
  const janInput = document.querySelector("#fieldJanInput");
  const retryButton = document.querySelector("#fieldRetryScan");
  const ocrButton = document.querySelector("#fieldOcrMode");
  const noJanButton = document.querySelector("#fieldNoJan");
  const result = document.querySelector("#fieldResult");
  const draftPanel = document.querySelector("#fieldNewDraft");
  const draftIdentity = document.querySelector("#fieldDraftIdentity");
  const tagPhoto = document.querySelector("#fieldTagPhoto");
  const draftName = document.querySelector("#fieldDraftName");
  const draftPrice = document.querySelector("#fieldDraftPrice");
  const draftStatus = document.querySelector("#fieldDraftStatus");
  const tagPreview = document.querySelector("#fieldTagPreview");
  const tagPreviewImage = document.querySelector("#fieldTagPreviewImage");
  const tagPreviewOpen = document.querySelector("#fieldTagPreviewOpen");
  const tagFileName = document.querySelector("#fieldTagFileName");
  const tagFileInfo = document.querySelector("#fieldTagFileInfo");
  const tagRetake = document.querySelector("#fieldTagRetake");
  const tagDelete = document.querySelector("#fieldTagDelete");
  const nextButton = document.querySelector("#fieldNext");
  const nextReason = document.querySelector("#fieldNextReason");
  const nextBar = document.querySelector(".field-next-bar");
  const autoNext = document.querySelector("#fieldAutoNext");
  const pendingCount = document.querySelector("#fieldPendingCount");
  const cacheBatchImagesButton = document.querySelector("#fieldCacheBatchImages");
  const cancelBatchImagesButton = document.querySelector("#fieldCancelBatchImages");
  const retryBatchImagesButton = document.querySelector("#fieldRetryBatchImages");
  const batchImageCacheStatus = document.querySelector("#fieldBatchImageCacheStatus");
  const cameraAdapter = new window.JBACamera.CameraAdapter();
  const unifiedScanner = new window.JBACamera.UnifiedJanScanner({
    video,
    placeholder,
    status: cameraStatus,
    cameraSelect,
    torchButton,
    zoomOut,
    zoomReset,
    zoomIn,
    debug,
    cameraAdapter,
    validJan,
    onCode: handleDetectedCode,
  });

  let dbPromise;
  let stream = null;
  let track = null;
  let detector = null;
  let zxingControls = null;
  let zxingReader = null;
  let zxingTimer = null;
  let zxingFrameId = null;
  let zxingWatchdogTimer = null;
  let zxingWatchdogRestarted = false;
  let scanTimer = null;
  let scanFrameId = null;
  let scanning = false;
  let scannerStartupStarted = false;
  let handlingCode = false;
  let pendingIdentity = null;
  let torchOn = false;
  let zoomValue = 1;
  let capabilities = {};
  let lastAcceptedCode = "";
  let lastAcceptedAt = 0;
  let autoNextTimer = null;
  let currentDraftOperationId = null;
  let currentTagPreviewUrl = null;
  let cacheAbortController = null;
  let failedCacheRows = [];
  let currentFetchController = null;
  let localLookupController = null;
  let localLookupKey = "";
  let localLookupPromise = null;
  let currentFlowGeneration = 0;
  let scanAudioContext = null;
  const flowState = {
    local_query_loading: false,
    local_product_found: false,
    local_product_not_found: false,
    local_query_failed_retryable: false,
    purchase_fact_saved: false,
    draft_saved: false,
    tag_local_saved: false,
    tag_sync_pending: false,
    online_enrichment_pending: false,
    online_enrichment_done: false,
    translation_pending: false,
    translation_done: false,
    failed_retryable: false,
  };
  const photoDiagnostics = {
    fileSelected: false,
    fileName: "",
    fileType: "",
    fileSize: 0,
    fileReadStarted: false,
    fileReadFinished: false,
    previewReady: false,
    indexedDbWriteStarted: false,
    indexedDbWriteSucceeded: false,
    localPhotoId: "",
    syncStatus: "idle",
    lastPhotoError: "",
  };
  const diagnostics = {
    buildVersion,
    zxingLoaded: false,
    loopRunning: false,
    decodeLoopActive: false,
    loopGeneration: 0,
    startupStage: "idle",
    startupException: "",
    zxingReaderCreated: false,
    zxingGlobalName: "missing",
    videoPlaying: false,
    loopStartCount: 0,
    loopStopCount: 0,
    lastStopReason: "",
    decodeAttempts: 0,
    framesTotal: 0,
    decodesPerSecond: 0,
    secondStartedAt: Date.now(),
    secondAttempts: 0,
    mode: "idle",
    lastException: "",
    cameraDiagnostic: null,
    localLookup: null,
  };

  function setFlowState(patch = {}) {
    Object.assign(flowState, patch);
    app.dataset.flowState = Object.entries(flowState)
      .filter(([, value]) => value)
      .map(([key]) => key)
      .join(" ");
  }

  function resetFlowState() {
    Object.keys(flowState).forEach((key) => { flowState[key] = false; });
    setFlowState();
  }

  function resetPhotoDiagnostics() {
    Object.assign(photoDiagnostics, {
      fileSelected: false,
      fileName: "",
      fileType: "",
      fileSize: 0,
      fileReadStarted: false,
      fileReadFinished: false,
      previewReady: false,
      indexedDbWriteStarted: false,
      indexedDbWriteSucceeded: false,
      localPhotoId: "",
      syncStatus: "idle",
      lastPhotoError: "",
    });
    updateDebug();
  }

  function validJan(value) {
    const digits = String(value || "").replace(/\D+/g, "");
    if (![8, 12, 13].includes(digits.length)) return null;
    const body = digits.slice(0, -1);
    const sum = [...body].reduce((total, digit, index) => (
      total + Number(digit) * (((body.length - index) % 2 === 0) ? 1 : 3)
    ), 0);
    if ((10 - (sum % 10)) % 10 !== Number(digits[digits.length - 1])) return null;
    return digits.length === 12 ? `0${digits}` : digits;
  }

  async function ensureBatch() {
    if (batchId) return batchId;
    const requestKey = localStorage.getItem(AUTO_BATCH_KEY) || requestId("auto-batch");
    localStorage.setItem(AUTO_BATCH_KEY, requestKey);
    const operatorInput = document.querySelector('form[action="/field-purchase/batches"] input[name="operator_name"]');
    const operatorName = operatorInput?.value.trim()
      || localStorage.getItem("jba.operator")
      || "现场采购";
    cameraStatus.textContent = "正在自动建立临时采购批次…";
    const payload = await fetchJsonWithTimeout("/api/field-purchase/batches/auto", {
      method: "POST",
      headers: {"Content-Type": "application/json", "Accept": "application/json"},
      body: JSON.stringify({client_request_id: requestKey, operator_name: operatorName}),
      errorPrefix: "自动建立采购批次失败",
    });
    batchId = Number(payload.id);
    batchNo = payload.batch_no;
    app.dataset.batchId = String(batchId);
    app.dataset.batchNo = batchNo;
    localStorage.setItem("jba.currentBatch", batchNo);
    localStorage.setItem("jba.operator", payload.operator_name);
    const badge = document.querySelector("#currentBatchBadge");
    if (badge) badge.textContent = batchNo;
    updateCacheAvailability();
    return batchId;
  }

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains("operations")) {
          const operations = db.createObjectStore("operations", {keyPath: "id"});
          operations.createIndex("batchId", "batchId");
          operations.createIndex("createdAt", "createdAt");
        }
        if (!db.objectStoreNames.contains("products")) {
          db.createObjectStore("products", {keyPath: "jan"});
        }
        if (!db.objectStoreNames.contains("meta")) {
          db.createObjectStore("meta", {keyPath: "key"});
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return dbPromise;
  }

  async function storeRequest(storeName, mode, action) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, mode);
      const store = tx.objectStore(storeName);
      let output;
      try {
        output = action(store, tx);
      } catch (error) {
        reject(error);
        return;
      }
      tx.oncomplete = () => resolve(output);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted"));
    });
  }

  async function idbGet(storeName, key) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const request = db.transaction(storeName).objectStore(storeName).get(key);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function idbAll(storeName) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const request = db.transaction(storeName).objectStore(storeName).getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function putOperation(operation) {
    await storeRequest("operations", "readwrite", (store) => store.put(operation));
    await refreshPendingCount();
  }

  async function deleteOperation(id) {
    await storeRequest("operations", "readwrite", (store) => store.delete(id));
    await refreshPendingCount();
  }

  async function cacheProduct(jan, product, signal = undefined) {
    if (!product) return;
    const cached = {
      jan,
      ...product,
      cachedAt: new Date().toISOString(),
    };
    if (product.image_url) {
      const controller = new AbortController();
      const abortFromCaller = () => controller.abort();
      if (signal) signal.addEventListener("abort", abortFromCaller, {once: true});
      const timeout = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS);
      try {
        const imageResponse = await fetch(product.image_url, {
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (imageResponse.ok) {
          cached.image_blob = await imageResponse.clone().blob();
          if (globalThis.caches && new URL(product.image_url, location.href).origin === location.origin) {
            const cache = await caches.open(IMAGE_CACHE_NAME);
            await cache.put(product.image_url, imageResponse.clone());
          }
        }
      } catch (_) {
      } finally {
        clearTimeout(timeout);
        if (signal) signal.removeEventListener("abort", abortFromCaller);
      }
    }
    if (jan) await storeRequest("products", "readwrite", (store) => store.put(cached));
  }

  async function refreshPendingCount() {
    const operations = (await idbAll("operations")).filter((item) => item.batchId === batchId);
    const count = operations.length;
    pendingCount.textContent = `待同步 ${count}`;
    const globalBadge = document.querySelector("#pendingSyncBadge");
    if (globalBadge) {
      globalBadge.textContent = count ? `待同步 ${count}` : "已同步";
      globalBadge.classList.toggle("has-pending", count > 0);
    }
    return count;
  }

  async function cacheCurrentBatchImages() {
    if (!cacheBatchImagesButton || !cancelBatchImagesButton || !retryBatchImagesButton) return;
    await ensureBatch();
    const rows = (failedCacheRows.length ? failedCacheRows : [...document.querySelectorAll(".field-batch-item[data-image-url]")])
      .filter((row) => row.dataset.imageUrl);
    if (!rows.length) {
      batchImageCacheStatus.textContent = "当前批次没有可缓存图片。";
      updateCacheAvailability();
      return;
    }
    cacheAbortController = new AbortController();
    cancelBatchImagesButton.hidden = false;
    retryBatchImagesButton.hidden = true;
    failedCacheRows = [];
    cacheBatchImagesButton.disabled = true;
    let completed = 0;
    let failed = 0;
    for (const row of rows) {
      if (cacheAbortController.signal.aborted) break;
      const estimate = navigator.storage?.estimate ? await navigator.storage.estimate() : {};
      const freeBytes = estimate.quota && estimate.usage ? estimate.quota - estimate.usage : null;
      const space = freeBytes == null ? "空间未知" : `剩余约 ${(freeBytes / 1024 / 1024).toFixed(0)} MB`;
      batchImageCacheStatus.textContent = `总数 ${rows.length} · 完成 ${completed} · 失败 ${failed} · ${space} · 正在处理 ${completed + failed + 1}/${rows.length}`;
      try {
        await cacheProduct(row.dataset.jan, {
          id: Number(row.dataset.productId),
          name: row.dataset.productName,
          image_url: row.dataset.imageUrl,
        }, cacheAbortController.signal);
        completed += 1;
      } catch (_) {
        failed += 1;
        failedCacheRows.push(row);
      }
    }
    if (cacheBatchImagesButton) cacheBatchImagesButton.disabled = false;
    if (cancelBatchImagesButton) cancelBatchImagesButton.hidden = true;
    if (retryBatchImagesButton) retryBatchImagesButton.hidden = failedCacheRows.length === 0;
    const cancelled = cacheAbortController.signal.aborted;
    cacheAbortController = null;
    batchImageCacheStatus.textContent = `${cancelled ? "已取消" : "完成"}：总数 ${rows.length}，完成 ${completed}，失败 ${failed}。`;
  }

  function updateCacheAvailability() {
    const rows = [...document.querySelectorAll(".field-batch-item")];
    const imageRows = rows.filter((row) => row.dataset.imageUrl);
    let reason = "";
    if (!batchId) reason = "尚未建立采购批次。";
    else if (!rows.length) reason = "当前批次没有商品。";
    else if (!imageRows.length) reason = "当前批次商品没有可下载图片。";
    if (!cacheBatchImagesButton) return;
    cacheBatchImagesButton.disabled = Boolean(reason);
    if (reason) batchImageCacheStatus.textContent = reason;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#039;",
    })[char]);
  }

  function safeErrorMessage(error, fallback = "请求失败，请重试。") {
    const name = error?.name || "";
    if (name === "AbortError" || String(error?.message || "").includes("timeout")) {
      return "请求超过 10 秒，请重试。";
    }
    const message = String(error?.safeMessage || error?.message || fallback)
      .replace(/https?:\/\/[^\s"']+/gi, "[url]")
      .replace(/[A-Za-z0-9_\-]{24,}/g, "[redacted]")
      .replace(/[\r\n\t]+/g, " ")
      .trim();
    return message.slice(0, 180) || fallback;
  }

  async function parseErrorResponse(response, prefix) {
    const statusText = `HTTP ${response.status}`;
    try {
      const payload = await response.clone().json();
      const detail = payload?.detail || payload?.message || payload?.error;
      if (detail) return `${prefix}：${statusText} ${detail}`;
    } catch (_) {}
    try {
      const text = (await response.text()).trim();
      if (text) return `${prefix}：${statusText} ${text.slice(0, 160)}`;
    } catch (_) {}
    return `${prefix}：${statusText}`;
  }

  async function fetchJsonWithTimeout(url, options = {}, timeoutMs = API_TIMEOUT_MS) {
    const controller = new AbortController();
    const {errorPrefix, signal: externalSignal, ...fetchOptions} = options;
    const abortFromExternal = () => controller.abort();
    if (externalSignal?.aborted) controller.abort();
    else externalSignal?.addEventListener("abort", abortFromExternal, {once: true});
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    currentFetchController = controller;
    try {
      const response = await fetch(url, {...fetchOptions, signal: controller.signal});
      if (!response.ok) {
        const error = new Error(await parseErrorResponse(response, errorPrefix || "请求失败"));
        error.status = response.status;
        error.safeMessage = error.message;
        throw error;
      }
      return response.json();
    } finally {
      clearTimeout(timeout);
      externalSignal?.removeEventListener("abort", abortFromExternal);
      if (currentFetchController === controller) currentFetchController = null;
    }
  }

  function setNextBarVisible(visible) {
    if (nextBar) nextBar.hidden = !visible;
  }

  function renderExisting(payload) {
    const product = payload.product;
    const imageUrl = product?.image_blob instanceof Blob
      ? URL.createObjectURL(product.image_blob)
      : product?.image_url;
    result.innerHTML = `
      <div class="field-result-head">
        ${imageUrl ? `<img src="${escapeHtml(imageUrl)}" alt="商品图片">` : "<div class=\"field-image-empty\">暂无图</div>"}
        <div><span class="badge success">商品已登记</span>
        <h2>${escapeHtml(product?.name || "已有商品")}</h2>
        <p>${escapeHtml(payload.jan)}</p></div>
      </div>
      <p class="field-batch-quantity">本批次数量 <strong>${Number(payload.quantity ?? payload.batch_quantity ?? 0)}</strong></p>`;
  }

  function renderAmbiguous(payload) {
    const cards = (payload.candidates || []).map((candidate) => `
      <article class="field-ambiguous-candidate">
        ${candidate.image_url ? `<img src="${escapeHtml(candidate.image_url)}" alt="商品图片">` : "<div class=\"field-image-empty\">暂无图</div>"}
        <div><h3>${escapeHtml(candidate.name || candidate.internal_sku)}</h3>
        <p>${escapeHtml(candidate.specification || "规格未填写")}</p>
        <p>秦丝货号：${escapeHtml(candidate.qinsi_product_code || "—")}</p>
        <p>秦丝条码：${escapeHtml((candidate.qinsi_barcodes || []).join("、") || "—")}</p>
        <button type="button" data-select-product="${Number(candidate.id)}">选择此商品</button></div>
      </article>`).join("");
    result.innerHTML = `
      <span class="badge warning">JAN 多匹配</span>
      <h2>${escapeHtml(payload.jan)}</h2>
      <p>${escapeHtml(payload.message)}</p>
      <div class="field-ambiguous-list">${cards}</div>
      <button type="button" class="secondary" data-store-review="true">暂存待审核</button>`;
    result.querySelectorAll("[data-select-product]").forEach((button) => {
      button.addEventListener("click", async () => {
        const product = (payload.candidates || []).find((item) => item.id === Number(button.dataset.selectProduct));
        if (!product) return;
        const saved = await queueExisting({...payload, product, batch_quantity: 0}, product.id);
        cameraStatus.textContent = saved ? "已按人工选择记录商品。" : "采购事实保存失败，请重试。";
      });
    });
    result.querySelector("[data-store-review]")?.addEventListener("click", async () => {
      await queueAmbiguousReview(payload);
      cameraStatus.textContent = "已暂存待审核，未绑定或新增商品。";
    });
  }

  function renderNew(jan, tempId) {
    const shortDraft = tempId ? `草稿 #${String(tempId).slice(-6).toUpperCase()}` : "";
    result.innerHTML = `
      <span class="badge warning">新商品</span>
      <h2>${escapeHtml(jan || shortDraft)}</h2>
      <p>吊牌照片必拍；名称、价格和商品照可以稍后补。</p>`;
    draftPanel.hidden = false;
    setNextBarVisible(true);
    pendingIdentity = {jan: jan || null, temporaryId: tempId || null};
    draftIdentity.textContent = jan ? `JAN：${jan}` : `${shortDraft}（内部草稿，不是 JAN/SKU）`;
    draftStatus.textContent = "请拍吊牌；保存完成前不能进入下一件。";
    setNextEnabled(false, "新品吊牌和草稿尚未写入本机。");
    tagPhoto.value = "";
    resetPhotoDiagnostics();
    clearTagPreview();
  }

  function renderInvalid(message) {
    result.innerHTML = `<span class="badge warning">条码未识别</span><h2>请选择处理方式</h2><p>${escapeHtml(message)}</p>`;
    setNextEnabled(false, "条码无效，尚未保存采购事实。");
  }

  function unlockScanAudio() {
    try {
      const AudioCtor = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtor) return null;
      if (!scanAudioContext) scanAudioContext = new AudioCtor();
      if (scanAudioContext.state === "suspended") {
        scanAudioContext.resume().catch(() => {});
      }
      return scanAudioContext;
    } catch (_) {
      return null;
    }
  }

  function playScanTone(kind = "success") {
    const context = unlockScanAudio();
    if (!context) return;
    try {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.frequency.value = kind === "success" ? 880 : 440;
      gain.gain.setValueAtTime(0.075, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.13);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.13);
    } catch (_) {}
  }

  function feedback(kind = "success") {
    if (navigator.vibrate) navigator.vibrate(kind === "success" ? [60] : [35, 40, 35]);
    playScanTone(kind);
  }

  function showScanFeedback(jan, kind = "success") {
    if (stage) stage.dataset.scanState = "";
    let toast = document.querySelector(".field-scan-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.className = "field-scan-toast";
      document.body.append(toast);
    }
    toast.textContent = `✓ 已识别 ${jan}`;
    toast.dataset.kind = kind;
    if (stage) {
      stage.dataset.scanState = kind;
      stage.classList.remove("scan-flash");
      void stage.offsetWidth;
      stage.classList.add("scan-flash");
    }
    window.clearTimeout(showScanFeedback.timer);
    showScanFeedback.timer = window.setTimeout(() => {
      if (stage) {
        stage.dataset.scanState = "";
        stage.classList.remove("scan-flash");
      }
      toast.textContent = "";
    }, SCAN_TOAST_MS);
  }

  function pauseDecodeAfterSuccess() {
    scanning = false;
    unifiedScanner.pauseAfterSuccess?.(SCAN_SUCCESS_PAUSE_MS);
    if (scanFrameId) cancelAnimationFrame(scanFrameId);
    if (scanTimer) clearTimeout(scanTimer);
    scanFrameId = null;
    scanTimer = null;
    stopZxingDecodeLoop("scan_success_pause");
    window.setTimeout(() => {
      diagnostics.lastStopReason = "scan_success_pause_elapsed";
      updateDebug();
    }, SCAN_SUCCESS_PAUSE_MS);
  }

  function abortLocalLookup() {
    localLookupController?.abort();
    localLookupController = null;
    localLookupKey = "";
    localLookupPromise = null;
    setFlowState({local_query_loading: false});
  }

  function nextItem() {
    if (autoNextTimer) clearInterval(autoNextTimer);
    autoNextTimer = null;
    currentFlowGeneration += 1;
    abortLocalLookup();
    currentFetchController?.abort();
    currentFetchController = null;
    handlingCode = false;
    pendingIdentity = null;
    janInput.value = "";
    draftPanel.hidden = true;
    draftName.value = "";
    draftPrice.value = "";
    tagPhoto.value = "";
    currentDraftOperationId = null;
    resetPhotoDiagnostics();
    clearTagPreview();
    resetFlowState();
    setNextBarVisible(false);
    if (manualSubmit) manualSubmit.disabled = false;
    setNextEnabled(false, "尚未可靠保存采购事实或新品草稿。");
    result.innerHTML = "<h2>等待扫码</h2><p class=\"muted\">现场查询只访问本地数据库，不等待平台或 AI。</p>";
    if (stream) {
      scanning = true;
      cameraStatus.textContent = "请将 JAN 放入框内。";
      if (detector) scanLoop();
      else if (zxingReader) startZxingDecodeLoop();
      else startZxingFallback().catch((error) => {
        rememberStartupException(error, "zxing_reset_start");
        cameraStatus.textContent = `自动识别组件启动失败（${error?.name || "Error"}），请重启相机后重试。`;
      });
    } else {
      startUnifiedScanner().catch(() => {});
    }
    janInput.focus();
  }

  function deriveNextItemState() {
    if (flowState.purchase_fact_saved) {
      return {enabled: true, reasonCode: "purchase_fact_saved", message: "采购事实已可靠保存，可以进入下一件。"};
    }
    if (flowState.draft_saved && flowState.tag_local_saved) {
      return {
        enabled: true,
        reasonCode: flowState.tag_sync_pending ? "local_saved_server_pending" : "draft_synced",
        message: flowState.tag_sync_pending ? "吊牌原图已保存到本机；服务器待同步，但可以进入下一件。" : "新品草稿和吊牌已可靠保存，可以进入下一件。",
      };
    }
    if (flowState.local_query_loading) {
      return {enabled: false, reasonCode: "local_query_loading", message: "正在查询本地商品。"};
    }
    if (flowState.failed_retryable) {
      return {enabled: false, reasonCode: "failed_retryable", message: "当前步骤失败，请重试后再进入下一件。"};
    }
    if (pendingIdentity && !flowState.tag_local_saved) {
      return {enabled: false, reasonCode: "tag_local_missing", message: "吊牌原图尚未可靠写入本机。"};
    }
    return {enabled: false, reasonCode: "not_saved", message: "尚未可靠保存采购事实或新品草稿。"};
  }

  function setNextEnabled(enabled, reason) {
    const state = enabled ? deriveNextItemState() : {enabled: false, reasonCode: "manual_block", message: reason};
    nextButton.disabled = !state.enabled;
    nextButton.dataset.reasonCode = state.reasonCode;
    nextReason.textContent = state.message;
  }

  function recordLookupDiagnostic(entry) {
    const safe = {
      jan: entry.jan || "",
      requestId: entry.requestId || "",
      endpoint: entry.endpoint || "/api/field-purchase/lookup",
      elapsedMs: Math.round(Number(entry.elapsedMs || 0)),
      httpStatus: entry.httpStatus ?? null,
      resultState: entry.resultState || "",
      matchSource: entry.matchSource || "",
      error: entry.error || "",
    };
    diagnostics.localLookup = safe;
    updateDebug();
    try {
      console.info("[field_purchase_lookup]", safe);
    } catch (_) {}
  }

  function resetScannerStats(mode = "idle") {
    diagnostics.zxingLoaded = Boolean(window.ZXingBrowser?.BrowserMultiFormatReader);
    diagnostics.loopRunning = false;
    diagnostics.decodeLoopActive = false;
    diagnostics.startupStage = mode;
    diagnostics.startupException = "";
    diagnostics.zxingReaderCreated = false;
    diagnostics.decodeAttempts = 0;
    diagnostics.framesTotal = 0;
    diagnostics.decodesPerSecond = 0;
    diagnostics.secondStartedAt = Date.now();
    diagnostics.secondAttempts = 0;
    diagnostics.mode = mode;
    diagnostics.lastException = "";
    diagnostics.cameraDiagnostic = null;
    zxingWatchdogRestarted = false;
    updateDebug();
  }

  function setStartupStage(stage, mode = stage) {
    diagnostics.startupStage = stage;
    diagnostics.mode = mode;
    updateDebug();
  }

  function rememberStartupException(error, stage = diagnostics.startupStage) {
    const name = error?.name || error?.constructor?.name || "Error";
    const message = String(error?.message || "").replace(/[\r\n\t]+/g, " ").slice(0, 160);
    diagnostics.startupException = `${stage}:${name}${message ? `:${message}` : ""}`;
    updateDebug();
  }

  function noteDecodeAttempt(mode) {
    diagnostics.mode = mode;
    diagnostics.startupStage = mode === "ZXing scanning" ? "zxing_scanning" : diagnostics.startupStage;
    diagnostics.loopRunning = true;
    diagnostics.decodeLoopActive = true;
    diagnostics.decodeAttempts += 1;
    diagnostics.framesTotal += 1;
    diagnostics.secondAttempts += 1;
    const now = Date.now();
    if (now - diagnostics.secondStartedAt >= 1000) {
      diagnostics.decodesPerSecond = diagnostics.secondAttempts;
      diagnostics.secondAttempts = 0;
      diagnostics.secondStartedAt = now;
    }
    updateDebug();
  }

  function rememberDecodeException(error) {
    const name = error?.name || error?.constructor?.name || "";
    if (name) diagnostics.lastException = name;
    updateDebug();
  }

  function isZxingNotFoundException(error) {
    const name = error?.name || error?.constructor?.name || "";
    const kind = typeof error?.getKind === "function" ? error.getKind() : error?.kind;
    return name === "NotFoundException"
      || kind === "NotFoundException"
      || String(error || "").includes("NotFoundException");
  }

  function noteDecodeMiss() {
    diagnostics.mode = "ZXing scanning";
    diagnostics.startupStage = "zxing_scanning";
    updateDebug();
  }

  function markVideoReady() {
    if (diagnostics.startupStage === "camera_starting") {
      setStartupStage("camera_playing");
      return;
    }
    updateDebug();
  }

  function waitForVideoFrame() {
    if (window.JBACamera.isVideoFrameReady(video)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        video.removeEventListener("loadedmetadata", check);
        video.removeEventListener("playing", check);
        video.removeEventListener("canplay", check);
      };
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        fn(value);
      };
      const timer = window.setTimeout(() => {
        const error = new Error(`empty video frame ${video.readyState}/${video.videoWidth}x${video.videoHeight}`);
        error.name = "EmptyVideoFrame";
        finish(reject, error);
      }, 5000);
      const check = () => {
        if (!window.JBACamera.isVideoFrameReady(video)) return;
        window.clearTimeout(timer);
        finish(resolve);
      };
      video.addEventListener("loadedmetadata", check);
      video.addEventListener("playing", check);
      video.addEventListener("canplay", check);
      check();
    });
  }

  function enableNext(auto = true) {
    setNextEnabled(true, "");
    if (autoNext.checked && auto) {
      let remaining = 1;
      nextReason.innerHTML = `${remaining}秒后继续 · <button type="button" class="link-button" data-cancel-auto-next>取消</button>`;
      autoNextTimer = window.setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
          clearInterval(autoNextTimer);
          autoNextTimer = null;
          if (!nextButton.disabled) nextButton.click();
        } else {
          nextReason.innerHTML = `${remaining}秒后继续 · <button type="button" class="link-button" data-cancel-auto-next>取消</button>`;
        }
      }, 1000);
    }
  }

  async function lookupJan(jan, generation) {
    const key = `${batchId}:${jan}`;
    if (localLookupPromise && localLookupKey === key) return localLookupPromise;
    abortLocalLookup();
    const controller = new AbortController();
    const lookupRequestId = requestId("lookup");
    const endpoint = "/api/field-purchase/lookup";
    const startedAt = performance.now();
    let httpStatus = null;
    localLookupController = controller;
    localLookupKey = key;
    setFlowState({
      local_query_loading: true,
      local_product_found: false,
      local_product_not_found: false,
      local_query_failed_retryable: false,
      failed_retryable: false,
    });
    localLookupPromise = (async () => {
      const url = `${endpoint}?code=${encodeURIComponent(jan)}&batch_id=${batchId}&request_id=${encodeURIComponent(lookupRequestId)}`;
      const payload = await fetchJsonWithTimeout(url, {
        headers: {"Accept": "application/json"},
        errorPrefix: "本地商品查询失败",
        signal: controller.signal,
      });
      if (generation !== currentFlowGeneration) return {...payload, stale: true};
      if (payload.product) await cacheProduct(jan, payload.product);
      httpStatus = 200;
      setFlowState({
        local_query_loading: false,
        local_product_found: payload.status === "UNIQUE",
        local_product_not_found: payload.status === "NOT_FOUND",
        local_query_failed_retryable: false,
      });
      recordLookupDiagnostic({
        jan,
        requestId: lookupRequestId,
        endpoint,
        elapsedMs: performance.now() - startedAt,
        httpStatus,
        resultState: payload.status,
        matchSource: payload.match_source || "",
      });
      return payload;
    })();
    try {
      return await localLookupPromise;
    } catch (error) {
      if (generation !== currentFlowGeneration || controller.signal.aborted) return {status: "STALE", jan, stale: true};
      httpStatus = error.status ?? null;
      const cached = await idbGet("products", jan);
      if (cached) {
        setFlowState({local_query_loading: false, local_product_found: true, local_query_failed_retryable: false});
        recordLookupDiagnostic({
          jan,
          requestId: lookupRequestId,
          endpoint,
          elapsedMs: performance.now() - startedAt,
          httpStatus,
          resultState: "UNIQUE",
          matchSource: "indexeddb_cache",
        });
        return {
          status: "UNIQUE",
          jan,
          product: cached,
          batch_quantity: 0,
          offline: true,
          message: "离线命中本机缓存",
        };
      }
      setFlowState({local_query_loading: false, local_query_failed_retryable: true, failed_retryable: true});
      recordLookupDiagnostic({
        jan,
        requestId: lookupRequestId,
        endpoint,
        elapsedMs: performance.now() - startedAt,
        httpStatus,
        resultState: "FAILED_RETRYABLE",
        error: safeErrorMessage(error, "lookup_failed"),
      });
      return {
        status: "FAILED_RETRYABLE",
        jan,
        message: safeErrorMessage(error, "本地商品查询失败，请重试。"),
      };
    } finally {
      if (localLookupKey === key) {
        localLookupController = null;
        localLookupKey = "";
        localLookupPromise = null;
        setFlowState({local_query_loading: false});
      }
    }
  }

  async function queueExisting(payload, selectedProductId = null) {
    const operation = {
      id: requestId("scan"),
      type: "existing",
      batchId,
      jan: payload.jan,
      product: payload.product,
      selectedProductId,
      status: "UPLOAD_PENDING",
      createdAt: new Date().toISOString(),
    };
    setNextEnabled(false, "正在保存采购事实。");
    try {
      const saved = await syncOperation(operation);
      await deleteOperation(operation.id);
      renderExisting(saved);
      result.insertAdjacentHTML("beforeend", "<p class=\"success message\">采购事实已保存。</p>");
      setFlowState({purchase_fact_saved: true, failed_retryable: false});
      feedback("success");
      enableNext();
      syncPending();
      return true;
    } catch (error) {
      operation.status = "FAILED_RETRYABLE";
      operation.lastError = safeErrorMessage(error, "采购事实保存失败。");
      await putOperation(operation);
      renderExisting({...payload, quantity: Number(payload.batch_quantity || 0)});
      result.insertAdjacentHTML("beforeend", `<p class="error message">采购事实保存失败：${escapeHtml(operation.lastError)}</p><button type="button" class="secondary compact" data-retry-operation="${escapeHtml(operation.id)}">重试保存</button>`);
      setFlowState({purchase_fact_saved: false, failed_retryable: true});
      setNextEnabled(false, "采购事实尚未保存成功，不能进入下一件。");
      feedback("warning");
      return false;
    }
  }

  async function retryOperation(id) {
    const operation = await idbGet("operations", id);
    if (!operation) {
      result.insertAdjacentHTML("beforeend", "<p class=\"warning message\">待重试记录不存在，可能已经同步。</p>");
      await refreshPendingCount();
      return;
    }
    setNextEnabled(false, "正在重试保存。");
    try {
      const payload = await syncOperation(operation);
      await deleteOperation(operation.id);
      if (operation.type === "existing") {
        renderExisting(payload);
        result.insertAdjacentHTML("beforeend", "<p class=\"success message\">采购事实已保存。</p>");
        setFlowState({purchase_fact_saved: true, failed_retryable: false});
        enableNext();
      } else if (operation.type === "new") {
        draftStatus.textContent = "服务器已同步；线上资料和 AI 补全在后台继续。";
        setFlowState({tag_sync_pending: false, failed_retryable: false, online_enrichment_pending: true});
        enableNext(false);
      }
      await refreshPendingCount();
    } catch (error) {
      operation.status = "FAILED_RETRYABLE";
      operation.lastError = safeErrorMessage(error, "重试失败。");
      await putOperation(operation);
      setNextEnabled(operation.type === "new" && flowState.draft_saved && flowState.tag_local_saved, operation.type === "new" ? "已本地保存，服务器待同步。" : "采购事实尚未保存成功，不能进入下一件。");
      result.insertAdjacentHTML("beforeend", `<p class="error message">重试失败：${escapeHtml(operation.lastError)}</p>`);
    }
  }

  async function queueAmbiguousReview(payload) {
    const operation = {
      id: requestId("ambiguous"),
      type: "ambiguous",
      batchId,
      jan: payload.jan,
      status: "UPLOAD_PENDING",
      createdAt: new Date().toISOString(),
    };
    await putOperation(operation);
    result.insertAdjacentHTML("beforeend", "<p class=\"success message\">已保存待审核，不会自动选品或建新品。</p>");
    feedback("warning");
    enableNext(false);
    syncPending();
  }

  async function handleCode(rawValue) {
    const jan = validJan(rawValue);
    if (jan && handlingCode && flowState.local_query_loading && janInput.value === jan) return;
    const generation = ++currentFlowGeneration;
    abortLocalLookup();
    handlingCode = true;
    if (manualSubmit) manualSubmit.disabled = true;
    scanning = false;
    unifiedScanner.scanning = false;
    if (!jan) {
      renderInvalid("不是校验位正确的 JAN-8/JAN-13。可重扫、拍吊牌识别 JAN、手输或登记无 JAN 商品。");
      feedback("warning");
      handlingCode = false;
      if (manualSubmit) manualSubmit.disabled = false;
      return;
    }
    try {
      await ensureBatch();
    } catch (_) {
      renderInvalid("临时采购批次建立失败，请确认网络后重试。");
      handlingCode = false;
      if (manualSubmit) manualSubmit.disabled = false;
      return;
    }
    janInput.value = jan;
    setNextBarVisible(true);
    draftPanel.hidden = true;
    pendingIdentity = null;
    result.innerHTML = `<span class="badge">查询中</span><h2>${escapeHtml(jan)}</h2><p class="muted">正在查询本地商品…</p>`;
    setNextEnabled(false, "正在查询本地商品。");
    cameraStatus.textContent = `已读取 ${jan}，正在查询本地商品…`;
    const payload = await lookupJan(jan, generation);
    if (payload.stale || generation !== currentFlowGeneration) return;
    if (payload.status === "UNIQUE") {
      const saved = await queueExisting(payload);
      cameraStatus.textContent = saved ? "商品已登记，采购事实已保存。" : "采购事实保存失败，请重试。";
    } else if (payload.status === "NOT_FOUND") {
      if (!flowState.local_query_loading) {
        renderNew(jan, null);
        cameraStatus.textContent = payload.offline ? "离线新商品：拍吊牌保存本机草稿。" : "新商品：请拍吊牌。";
        feedback("warning");
      }
    } else if (payload.status === "AMBIGUOUS") {
      renderAmbiguous(payload);
      cameraStatus.textContent = "JAN 多匹配：请选择商品或暂存待审核。";
      feedback("warning");
    } else {
      renderInvalid(payload.message || "本地商品查询失败，请重试。");
      result.insertAdjacentHTML("beforeend", `<button type="button" class="secondary compact" data-retry-lookup="${escapeHtml(jan)}">重试查询本地商品</button>`);
      setFlowState({failed_retryable: true});
      setNextEnabled(false, "本地查询失败，尚未保存采购事实或新品草稿。");
      feedback("warning");
      handlingCode = false;
      if (manualSubmit) manualSubmit.disabled = false;
    }
  }

  function handleDetectedCode(rawValue) {
    const jan = validJan(rawValue);
    if (!jan) return;
    const now = Date.now();
    if (jan === lastAcceptedCode && now - lastAcceptedAt < SAME_CODE_DEBOUNCE_MS) return;
    lastAcceptedCode = jan;
    lastAcceptedAt = now;
    showScanFeedback(jan, "success");
    pauseDecodeAfterSuccess();
    feedback("success");
    handleCode(jan);
  }

  async function syncOperation(operation) {
    if (operation.type === "existing") {
      const payload = await fetchJsonWithTimeout("/api/field-purchase/scans", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        body: JSON.stringify({
          batch_id: operation.batchId,
          jan: operation.jan,
          quantity: 1,
          client_request_id: operation.id,
          selected_product_id: operation.selectedProductId,
        }),
        errorPrefix: "采购事实保存失败",
      });
      if (payload.product) await cacheProduct(operation.jan, payload.product);
      return payload;
    }
    if (operation.type === "ambiguous") {
      return fetchJsonWithTimeout("/api/field-purchase/scans", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        body: JSON.stringify({
          batch_id: operation.batchId,
          jan: operation.jan,
          quantity: 1,
          client_request_id: operation.id,
          review_only: true,
        }),
        errorPrefix: "待审核保存失败",
      });
    }
    const form = new FormData();
    form.set("batch_id", String(operation.batchId));
    form.set("client_request_id", operation.id);
    form.set("jan", operation.jan || "");
    form.set("temporary_id", operation.temporaryId || "");
    form.set("name", operation.name || "");
    form.set("unit_price", operation.unitPrice == null ? "" : String(operation.unitPrice));
    form.set("quantity", "1");
    form.set("tag_photo", operation.photo, operation.photoName || "tag-photo.jpg");
    return fetchJsonWithTimeout("/api/field-purchase/drafts", {
      method: "POST",
      headers: {"Accept": "application/json"},
      body: form,
      errorPrefix: "新品草稿同步失败",
    });
  }

  async function syncPending() {
    if (!navigator.onLine) {
      await refreshPendingCount();
      return;
    }
    const operations = (await idbAll("operations"))
      .filter((item) => item.batchId === batchId)
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    for (const operation of operations) {
      try {
        await syncOperation(operation);
        await deleteOperation(operation.id);
      } catch (error) {
        operation.status = "FAILED_RETRYABLE";
        operation.lastError = safeErrorMessage(error, "同步失败。").slice(0, 300);
        operation.retryAt = new Date(Date.now() + 5000).toISOString();
        await putOperation(operation);
        break;
      }
    }
    await refreshPendingCount();
  }

  function clearTagPreview() {
    if (currentTagPreviewUrl) URL.revokeObjectURL(currentTagPreviewUrl);
    currentTagPreviewUrl = null;
    tagPreview.hidden = true;
    tagPreviewImage.removeAttribute("src");
    tagFileName.textContent = "";
    tagFileInfo.textContent = "";
  }

  function renderTagPreview(photo) {
    clearTagPreview();
    try {
      currentTagPreviewUrl = URL.createObjectURL(photo);
      tagPreviewImage.src = currentTagPreviewUrl;
      tagPreview.hidden = false;
      tagFileName.textContent = photo.name || "吊牌照片";
      tagFileInfo.textContent = `${photo.type || "HEIC/JPEG Blob"} · ${(photo.size / 1024).toFixed(1)} KB`;
      photoDiagnostics.previewReady = true;
    } catch (error) {
      photoDiagnostics.previewReady = false;
      photoDiagnostics.lastPhotoError = safeErrorMessage(error, "预览生成失败，但原始照片仍会保存。");
      draftStatus.textContent = photoDiagnostics.lastPhotoError;
    }
    updateDebug();
  }

  async function verifyPhotoReadable(photo) {
    photoDiagnostics.fileReadStarted = true;
    updateDebug();
    if (typeof photo.arrayBuffer === "function") {
      await photo.arrayBuffer();
    } else {
      await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = resolve;
        reader.onerror = () => reject(reader.error || new Error("FileReader failed"));
        reader.readAsArrayBuffer(photo);
      });
    }
    photoDiagnostics.fileReadFinished = true;
    updateDebug();
  }

  tagPreviewOpen.addEventListener("click", () => {
    if (!currentTagPreviewUrl) return;
    const dialog = document.createElement("dialog");
    dialog.className = "field-tag-dialog";
    dialog.innerHTML = `<button type="button" aria-label="关闭">×</button><img src="${escapeHtml(currentTagPreviewUrl)}" alt="吊牌大图">`;
    dialog.querySelector("button").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    if (dialog.showModal) dialog.showModal();
    else window.open(currentTagPreviewUrl, "_blank", "noopener");
  });
  tagRetake.addEventListener("click", () => tagPhoto.click());
  tagDelete.addEventListener("click", async () => {
    if (currentDraftOperationId) await deleteOperation(currentDraftOperationId);
    currentDraftOperationId = null;
    tagPhoto.value = "";
    clearTagPreview();
    draftStatus.textContent = "吊牌已删除，请重拍后再继续。";
    setNextEnabled(false, "新品吊牌和草稿尚未写入本机。");
  });

  tagPhoto.addEventListener("click", () => stopUnifiedScanner("拍摄吊牌后将立即保存到本机。", "tag_photo"));
  tagPhoto.addEventListener("change", async () => {
    const photo = tagPhoto.files?.[0];
    if (!photo || !pendingIdentity) return;
    resetPhotoDiagnostics();
    Object.assign(photoDiagnostics, {
      fileSelected: true,
      fileName: photo.name || "tag-photo",
      fileType: photo.type || "",
      fileSize: photo.size || 0,
      syncStatus: "local_saving",
    });
    draftStatus.textContent = "正在保存";
    renderTagPreview(photo);
    const operation = {
      id: requestId("draft"),
      type: "new",
      batchId,
      jan: pendingIdentity.jan,
      temporaryId: pendingIdentity.temporaryId,
      name: draftName.value.trim(),
      unitPrice: draftPrice.value === "" ? null : Number(draftPrice.value),
      photo,
      photoName: photo.name || "tag-photo.jpg",
      photoType: photo.type,
      status: "LOCAL_DRAFT",
      createdAt: new Date().toISOString(),
    };
    try {
      await verifyPhotoReadable(photo);
      photoDiagnostics.indexedDbWriteStarted = true;
      photoDiagnostics.localPhotoId = operation.id;
      updateDebug();
      await putOperation(operation);
      photoDiagnostics.indexedDbWriteSucceeded = true;
      photoDiagnostics.syncStatus = "server_pending";
      currentDraftOperationId = operation.id;
      setFlowState({
        draft_saved: true,
        tag_local_saved: true,
        tag_sync_pending: true,
        online_enrichment_pending: true,
        failed_retryable: false,
      });
      draftStatus.textContent = "已本地保存，服务器待同步。";
      result.insertAdjacentHTML("beforeend", `<p class="success message">本地草稿和吊牌已保存，可以下一件。</p><button type="button" class="secondary compact" data-retry-operation="${escapeHtml(operation.id)}">重试服务器同步</button>`);
      feedback("success");
      enableNext();
      syncPending().then(async () => {
        const pending = await idbGet("operations", operation.id);
        if (!pending) {
          setFlowState({tag_sync_pending: false});
          photoDiagnostics.syncStatus = "server_synced";
          draftStatus.textContent = "服务器已同步；线上资料和 AI 补全在后台继续。";
        } else if (pending.status === "FAILED_RETRYABLE") {
          setFlowState({tag_sync_pending: true, failed_retryable: true});
          photoDiagnostics.syncStatus = "server_pending";
          photoDiagnostics.lastPhotoError = safeErrorMessage(pending.lastError);
          draftStatus.textContent = `已本地保存，服务器待同步：${safeErrorMessage(pending.lastError)}`;
        }
        updateDebug();
      });
    } catch (error) {
      setFlowState({draft_saved: false, tag_local_saved: false, failed_retryable: true});
      photoDiagnostics.syncStatus = "local_failed";
      photoDiagnostics.lastPhotoError = safeErrorMessage(error, "请保留页面并重拍。");
      draftStatus.textContent = `吊牌本地保存失败：${safeErrorMessage(error, "请保留页面并重拍。")}`;
      setNextEnabled(false, "吊牌原图尚未可靠写入本机。");
      feedback("warning");
    } finally {
      tagPhoto.value = "";
      updateDebug();
    }
  });

  manualForm.addEventListener("submit", (event) => {
    event.preventDefault();
    handleCode(janInput.value);
  });
  result.addEventListener("click", (event) => {
    const button = event.target.closest("[data-retry-operation]");
    if (button) {
      retryOperation(button.dataset.retryOperation);
      return;
    }
    const retryLookup = event.target.closest("[data-retry-lookup]");
    if (retryLookup) handleCode(retryLookup.dataset.retryLookup);
  });
  nextReason.addEventListener("click", (event) => {
    if (!event.target.closest("[data-cancel-auto-next]")) return;
    if (autoNextTimer) clearInterval(autoNextTimer);
    autoNextTimer = null;
    nextReason.textContent = deriveNextItemState().message;
  });
  retryButton.addEventListener("click", nextItem);
  ocrButton.addEventListener("click", async () => {
    try {
      await ensureBatch();
      const tempId = `TMP-${requestId("ocr").replace(/^ocr-/, "")}`;
      renderNew(null, tempId);
      draftStatus.textContent = "请拍吊牌；OCR 当前未配置，照片保存后将进入后台待处理。";
    } catch (error) {
      renderInvalid(safeErrorMessage(error, "临时采购批次建立失败，请重试。"));
    }
  });
  noJanButton.addEventListener("click", async () => {
    const currentJan = validJan(janInput.value);
    if (currentJan && !window.confirm(`当前已有合法 JAN ${currentJan}。确认仍按“无 JAN 商品”建立草稿？`)) return;
    try {
      await ensureBatch();
      const tempId = `TMP-${requestId("item").replace(/^item-/, "")}`;
      renderNew(null, tempId);
    } catch (error) {
      renderInvalid(safeErrorMessage(error, "临时采购批次建立失败，请重试。"));
    }
  });
  nextButton.addEventListener("click", nextItem);
  autoNext.checked = localStorage.getItem(AUTO_NEXT_KEY) === "true";
  autoNext.addEventListener("change", () => localStorage.setItem(AUTO_NEXT_KEY, String(autoNext.checked)));
  cacheBatchImagesButton?.addEventListener("click", cacheCurrentBatchImages);
  cancelBatchImagesButton?.addEventListener("click", () => cacheAbortController?.abort());
  retryBatchImagesButton?.addEventListener("click", cacheCurrentBatchImages);
  window.addEventListener("online", syncPending);

  function stopZxingDecodeLoop(reason) {
    if (zxingTimer) clearTimeout(zxingTimer);
    if (zxingFrameId) cancelAnimationFrame(zxingFrameId);
    if (zxingWatchdogTimer) clearTimeout(zxingWatchdogTimer);
    if (diagnostics.decodeLoopActive || zxingTimer || zxingFrameId) {
      diagnostics.loopStopCount += 1;
    }
    diagnostics.loopGeneration += 1;
    zxingTimer = null;
    zxingFrameId = null;
    zxingWatchdogTimer = null;
    diagnostics.loopRunning = false;
    diagnostics.decodeLoopActive = false;
    diagnostics.lastStopReason = reason || "unknown";
    diagnostics.startupStage = "zxing_stopped";
    diagnostics.mode = "zxing_stopped";
    updateDebug();
  }

  function stopTracks(reason = "stop_tracks") {
    if (scanFrameId) cancelAnimationFrame(scanFrameId);
    if (scanTimer) clearTimeout(scanTimer);
    stopZxingDecodeLoop(reason);
    scanFrameId = null;
    scanTimer = null;
    scanning = false;
    scannerStartupStarted = false;
    zxingControls?.stop?.();
    zxingControls = null;
    zxingReader?.reset?.();
    zxingReader = null;
    diagnostics.zxingReaderCreated = false;
    unifiedScanner.stop(reason);
    stream = null;
    track = null;
    capabilities = {};
    torchOn = false;
    torchButton.hidden = true;
    torchButton.disabled = true;
    torchButton.textContent = "补光灯";
    video.srcObject = null;
    video.classList.remove("active");
    placeholder.hidden = false;
    updateDebug();
  }

  function stopCamera(message = "扫码已停止，可手动输入。", reason = "user_stop") {
    stopTracks(reason);
    startButton.disabled = false;
    stopButton.disabled = true;
    cameraStatus.textContent = message;
  }

  async function renderCameraOptions(devices) {
    cameraSelect.replaceChildren();
    for (const [index, device] of devices.entries()) {
      const option = document.createElement("option");
      option.value = device.deviceId;
      option.textContent = device.label || `摄像头 ${index + 1}`;
      cameraSelect.append(option);
    }
    cameraSelect.hidden = devices.length < 2;
    const currentId = track?.getSettings?.().deviceId;
    if (currentId) cameraSelect.value = currentId;
    const hasZoom = Boolean(capabilities.zoom);
    zoomOut.hidden = zoomReset.hidden = zoomIn.hidden = !hasZoom;
    torchButton.hidden = !cameraAdapter.torchSupported();
    torchButton.disabled = !cameraAdapter.torchSupported();
    updateDebug();
  }

  function refreshDiagnosticsRuntime() {
    diagnostics.zxingLoaded = zxingReady();
    diagnostics.zxingGlobalName = zxingGlobalName();
    diagnostics.videoPlaying = Boolean(video && !video.paused && !video.ended && video.readyState >= 2);
    return diagnostics;
  }

  function diagnosticsSnapshot() {
    const current = refreshDiagnosticsRuntime();
    const settings = track?.getSettings?.() || {};
    const selected = cameraSelect.selectedOptions?.[0];
    return {
      buildVersion: current.buildVersion,
      startupStage: current.startupStage,
      mode: current.mode,
      decodeLoopActive: current.decodeLoopActive,
      zxingReaderCreated: current.zxingReaderCreated,
      zxingGlobalName: current.zxingGlobalName,
      videoPlaying: current.videoPlaying,
      loopStartCount: current.loopStartCount,
      loopStopCount: current.loopStopCount,
      loopGeneration: current.loopGeneration,
      framesTotal: current.framesTotal,
      decodesPerSecond: current.decodesPerSecond,
      lastException: current.lastException || "none",
      startupException: current.startupException || "none",
      lastStopReason: current.lastStopReason || "none",
      zxing: current.zxingLoaded ? "ZXing loaded" : "ZXing missing",
      decodeLoop: current.loopRunning ? "decode loop running" : "decode loop stopped",
      readyState: video.readyState,
      videoWidth: video.videoWidth,
      videoHeight: video.videoHeight,
      deviceId: settings.deviceId,
      label: selected?.textContent || track?.label || "",
      video: `${video.videoWidth}×${video.videoHeight}`,
      settingsResolution: `${settings.width || "?"}×${settings.height || "?"}`,
      frameRate: settings.frameRate,
      zoom: settings.zoom ?? 1,
      focusMode: settings.focusMode || "unsupported",
      resizeMode: settings.resizeMode || "unsupported",
      photo: {...photoDiagnostics},
      cameraDiagnostic: current.cameraDiagnostic || undefined,
    };
  }

  function renderDiagnostics() {
    if (!debug) return;
    debug.hidden = false;
    copyDiagnosticButton.hidden = false;
    refreshDiagnosticButton.hidden = false;
    debug.textContent = JSON.stringify(diagnosticsSnapshot(), null, 2);
  }

  function updateDebug() {
    renderDiagnostics();
  }

  async function detectWithBarcodeDetector() {
    return window.JBACamera.detectBarcode(detector, video);
  }

  function zxingApi() {
    return {
      Reader: window.ZXingBrowser?.BrowserMultiFormatReader,
      formats: window.ZXingBrowser?.BarcodeFormat,
    };
  }

  function zxingGlobalName() {
    const api = zxingApi();
    if (api.Reader && api.formats) return "ZXingBrowser.BrowserMultiFormatReader";
    if (window.ZXingBrowser) return "ZXingBrowser";
    return "missing";
  }

  function zxingReady() {
    const api = zxingApi();
    return Boolean(api.Reader && api.formats);
  }

  function scheduleZxingStartupWatchdog(loopId) {
    if (zxingWatchdogTimer) window.clearTimeout(zxingWatchdogTimer);
    zxingWatchdogTimer = window.setTimeout(() => {
      zxingWatchdogTimer = null;
      if (
        zxingWatchdogRestarted
        || detector
        || !stream
        || !zxingReader
        || loopId !== diagnostics.loopGeneration
        || !["zxing_ready", "zxing_loop_starting", "zxing_scanning"].includes(diagnostics.startupStage)
        || diagnostics.framesTotal > 0
      ) return;
      zxingWatchdogRestarted = true;
      diagnostics.lastStopReason = "watchdog_frames_still_zero";
      stopZxingDecodeLoop("watchdog_frames_still_zero");
      startZxingDecodeLoop();
    }, ZXING_STARTUP_WATCHDOG_MS);
  }

  function scheduleZxingReadyWatchdog() {
    if (zxingWatchdogTimer) window.clearTimeout(zxingWatchdogTimer);
    zxingWatchdogTimer = window.setTimeout(() => {
      zxingWatchdogTimer = null;
      if (
        zxingWatchdogRestarted
        || detector
        || !stream
        || !zxingReader
        || diagnostics.decodeLoopActive
        || diagnostics.startupStage !== "zxing_ready"
        || diagnostics.framesTotal > 0
      ) return;
      zxingWatchdogRestarted = true;
      diagnostics.lastStopReason = "watchdog_zxing_ready_no_frames";
      startZxingDecodeLoop();
    }, ZXING_STARTUP_WATCHDOG_MS);
  }

  function maybeStartZxingFallback(reason) {
    if (scannerStartupStarted || !scanning || detector || diagnostics.decodeLoopActive || zxingReader || !stream) return;
    if (!zxingReady()) return;
    startZxingFallback().catch((error) => {
      rememberStartupException(error, reason || "zxing_late_start");
      cameraStatus.textContent = `自动识别组件启动失败（${error?.name || "Error"}），请重启相机后重试。`;
    });
  }

  function ensureZxingLoaded() {
    setStartupStage("zxing_loading");
    diagnostics.zxingLoaded = zxingReady();
    if (diagnostics.zxingLoaded) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const startedAt = Date.now();
      let settled = false;
      const script = [...document.scripts].find((item) => item.src && item.src.includes("/static/vendor/zxing/"));
      const cleanup = () => {
        window.clearInterval(timer);
        window.clearTimeout(deadline);
        script?.removeEventListener("load", check);
        script?.removeEventListener("error", onScriptError);
      };
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        fn(value);
      };
      const onScriptError = () => {
        const error = new Error("ZXing script failed to load");
        error.name = "ZXingScriptError";
        finish(reject, error);
      };
      const check = () => {
        diagnostics.zxingLoaded = zxingReady();
        if (diagnostics.zxingLoaded) finish(resolve);
        else if (Date.now() - startedAt > ZXING_LOAD_TIMEOUT_MS) {
          const error = new Error("ZXing reader global was not available before timeout");
          error.name = "ZXingLoadTimeout";
          finish(reject, error);
        } else {
          updateDebug();
        }
      };
      const timer = window.setInterval(check, 100);
      const deadline = window.setTimeout(check, ZXING_LOAD_TIMEOUT_MS + 20);
      script?.addEventListener("load", check);
      script?.addEventListener("error", onScriptError);
      check();
    });
  }

  async function createZxingReader() {
    await ensureZxingLoaded();
    const {Reader, formats} = zxingApi();
    if (!Reader || !formats) throw new Error("ZXingUnavailable");
    try {
      zxingReader = new Reader(undefined, {
        delayBetweenScanAttempts: SCAN_INTERVAL_MS,
        delayBetweenScanSuccess: SAME_CODE_DEBOUNCE_MS,
      });
      zxingReader.possibleFormats = [formats.EAN_13, formats.EAN_8, formats.UPC_A];
      diagnostics.zxingReaderCreated = true;
      diagnostics.zxingLoaded = true;
      setStartupStage("zxing_ready");
      scheduleZxingReadyWatchdog();
      return zxingReader;
    } catch (error) {
      diagnostics.zxingReaderCreated = false;
      rememberStartupException(error, "zxing_reader_create");
      throw error;
    }
  }

  async function scanLoop() {
    if (!scanning || !detector) return;
    if (!window.JBACamera.isVideoFrameReady(video)) {
      rememberDecodeException({name: `EmptyVideoFrame:${video.readyState}/${video.videoWidth}x${video.videoHeight}`});
      scanTimer = window.setTimeout(scanLoop, SCAN_INTERVAL_MS);
      return;
    }
    try {
      noteDecodeAttempt("BarcodeDetector full frame");
      const codes = await detectWithBarcodeDetector();
      const value = codes.map((code) => code.rawValue).find((item) => validJan(item));
      if (value) {
        handleDetectedCode(value);
        return;
      }
    } catch (error) {
      rememberDecodeException(error);
      cameraStatus.textContent = "正在识别；请让条码完整进入画面并保持稳定。";
    }
    scanTimer = window.setTimeout(scanLoop, SCAN_INTERVAL_MS);
  }

  async function startZxingFallback() {
    await createZxingReader();
    await waitForVideoFrame();
    startZxingDecodeLoop();
    cameraStatus.textContent = "ZXing loaded；decode loop running。";
  }

  async function startScannerAfterPlaying(reason) {
    if (scannerStartupStarted || !stream) return;
    scannerStartupStarted = true;
    try {
      try {
        detector = await window.JBACamera.createBarcodeDetector();
      } catch (error) {
        detector = null;
        rememberStartupException(error, "barcode_detector_create");
      }
      scanning = true;
      setStartupStage("camera_playing");
      cameraStatus.textContent = "请将 JAN 放入框内；点击画面可尝试对焦。";
      if (detector) {
        diagnostics.mode = "BarcodeDetector scanning";
        await waitForVideoFrame();
        scanLoop();
        return;
      }
      await startZxingFallback(reason);
    } catch (error) {
      scannerStartupStarted = false;
      rememberStartupException(error, diagnostics.startupStage || reason || "scanner_start");
      cameraStatus.textContent = `自动识别组件启动失败（${error?.name || "Error"}），请重启相机后重试。`;
    }
  }

  function startZxingDecodeLoop() {
    if (!zxingReader) {
      const error = new Error("ZXing reader not created");
      error.name = "ZXingReaderMissing";
      rememberStartupException(error, "zxing_loop_starting");
      return;
    }
    stopZxingDecodeLoop("zxing_loop_restart");
    setStartupStage("zxing_loop_starting");
    const loopId = ++diagnostics.loopGeneration;
    diagnostics.loopStartCount += 1;
    scanning = true;
    diagnostics.decodeLoopActive = true;
    diagnostics.loopRunning = true;
    diagnostics.startupStage = "zxing_scanning";
    diagnostics.mode = "ZXing scanning";
    diagnostics.lastStopReason = "";
    updateDebug();
    scheduleZxingStartupWatchdog(loopId);
    const decodeWholeFrame = () => {
      zxingFrameId = null;
      if (!scanning || !diagnostics.decodeLoopActive || loopId !== diagnostics.loopGeneration || !zxingReader) return;
      try {
        noteDecodeAttempt("ZXing scanning");
        if (!window.JBACamera.isVideoFrameReady(video)) {
          rememberDecodeException({name: `EmptyVideoFrame:${video.readyState}/${video.videoWidth}x${video.videoHeight}`});
          return;
        }
        const decoded = zxingReader.decode(video);
        const value = decoded?.getText?.() || decoded?.text;
        if (value) {
          handleDetectedCode(value);
          return;
        }
      } catch (error) {
        if (isZxingNotFoundException(error)) noteDecodeMiss();
        else rememberDecodeException(error);
      } finally {
        if (scanning && diagnostics.decodeLoopActive && loopId === diagnostics.loopGeneration && zxingReader) {
          zxingTimer = window.setTimeout(() => {
            zxingTimer = null;
            if (!scanning || !diagnostics.decodeLoopActive || loopId !== diagnostics.loopGeneration || !zxingReader) return;
            if (window.requestAnimationFrame) {
              zxingFrameId = window.requestAnimationFrame(decodeWholeFrame);
            } else {
              decodeWholeFrame();
            }
          }, SCAN_INTERVAL_MS);
        }
      }
    };
    decodeWholeFrame();
  }

  async function startCamera(deviceId = "") {
    stopTracks(deviceId ? "camera_switch" : "camera_restart");
    resetScannerStats("camera_starting");
    setStartupStage("camera_starting");
    scannerStartupStarted = false;
    startButton.disabled = true;
    cameraStatus.textContent = "正在请求后置摄像头…";
    try {
      const state = await cameraAdapter.start(deviceId, {onStream: async (nextStream) => {
        stream = nextStream;
        track = nextStream.getVideoTracks()[0];
        video.srcObject = nextStream;
        await video.play();
        await waitForVideoFrame();
        setStartupStage("camera_playing");
        video.classList.add("active");
        placeholder.hidden = true;
        if (cameraAdapter.platform?.isIOSWebKit) {
          startScannerAfterPlaying("ios_video_playing").catch((error) => {
            rememberStartupException(error, "ios_video_playing");
          });
        }
      }});
      stream = state.stream;
      track = state.track;
      capabilities = state.capabilities;
      zoomValue = state.settings.zoom ?? 1;
      stopButton.disabled = false;
      await renderCameraOptions(state.devices);
      await startScannerAfterPlaying("camera_start_complete");
      copyDiagnosticButton.hidden = false;
      refreshDiagnosticButton.hidden = false;
      updateDebug();
    } catch (error) {
      const details = window.JBACamera.cameraErrorDetails(error, {
        secureContext: globalThis.isSecureContext,
        platform: cameraAdapter.platform,
        mediaDevices: navigator.mediaDevices,
      });
      rememberStartupException(error, diagnostics.startupStage || "camera_starting");
      stopCamera(details.message, "camera_start_error");
      diagnostics.cameraDiagnostic = details.diagnostic;
      updateDebug();
      copyDiagnosticButton.hidden = false;
      refreshDiagnosticButton.hidden = false;
    }
  }

  function syncUnifiedCameraState() {
    stream = unifiedScanner.stream;
    track = unifiedScanner.track;
    capabilities = unifiedScanner.capabilities || {};
    zoomValue = unifiedScanner.zoomValue || 1;
    scanning = unifiedScanner.scanning;
    diagnostics.zxingLoaded = unifiedScanner.stats.zxingLoaded;
    diagnostics.zxingReaderCreated = unifiedScanner.stats.zxingReaderCreated;
    diagnostics.loopRunning = unifiedScanner.stats.loopRunning;
    diagnostics.decodeAttempts = unifiedScanner.stats.decodeAttempts;
    diagnostics.framesTotal = unifiedScanner.stats.framesTotal;
    diagnostics.decodesPerSecond = unifiedScanner.stats.decodesPerSecond;
    diagnostics.mode = unifiedScanner.stats.mode;
    diagnostics.lastException = unifiedScanner.stats.lastException;
    diagnostics.lastStopReason = unifiedScanner.stats.lastStopReason;
    updateDebug();
  }

  async function startUnifiedScanner(deviceId = "") {
    stopTracks(deviceId ? "camera_switch" : "camera_restart");
    resetScannerStats("unified_camera_starting");
    startButton.disabled = true;
    try {
      await unifiedScanner.start(deviceId);
      syncUnifiedCameraState();
      startButton.disabled = true;
      stopButton.disabled = false;
      copyDiagnosticButton.hidden = true;
      refreshDiagnosticButton.hidden = false;
    } catch (error) {
      const details = window.JBACamera.cameraErrorDetails(error, {
        secureContext: globalThis.isSecureContext,
        platform: cameraAdapter.platform,
        mediaDevices: navigator.mediaDevices,
      });
      diagnostics.cameraDiagnostic = details.diagnostic;
      rememberStartupException(error, "unified_camera_start");
      stopUnifiedScanner(details.message, "camera_start_error");
      copyDiagnosticButton.hidden = false;
      refreshDiagnosticButton.hidden = false;
    }
  }

  function stopUnifiedScanner(message = "扫码已停止，可手动输入。", reason = "user_stop") {
    stopTracks(reason);
    if (message) cameraStatus.textContent = message;
    startButton.disabled = false;
    stopButton.disabled = true;
    syncUnifiedCameraState();
  }

  async function setZoom(nextValue) {
    const applied = await unifiedScanner.setZoom(nextValue);
    if (applied !== null) {
      zoomValue = applied;
      capabilities = unifiedScanner.capabilities;
    }
  }

  stage.addEventListener("click", async (event) => {
    if (!unifiedScanner.track) return;
    const rect = video.getBoundingClientRect();
    const point = {
      x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    };
    if (await unifiedScanner.focusAt(point)) {
      cameraStatus.textContent = "已请求点击对焦。";
      window.setTimeout(() => { if (unifiedScanner.stream) cameraStatus.textContent = "请将 JAN 放入框内。"; }, 800);
    }
  });

  startButton.addEventListener("click", () => {
    unlockScanAudio();
    cameraStatus.textContent = "正在请求摄像头…";
    startUnifiedScanner();
  });
  stopButton.addEventListener("click", () => stopUnifiedScanner("扫码已停止，可手动输入。", "user_stop"));
  cameraSelect.addEventListener("change", () => startUnifiedScanner(cameraSelect.value));
  torchButton.addEventListener("click", async () => {
    const result = await unifiedScanner.setTorch(!torchOn);
    torchOn = result.enabled;
  });
  zoomOut.addEventListener("click", () => setZoom(zoomValue - (capabilities.zoom?.step || 0.2)));
  zoomReset.addEventListener("click", () => setZoom(1));
  zoomIn.addEventListener("click", () => setZoom(zoomValue + (capabilities.zoom?.step || 0.2)));
  video.addEventListener("loadedmetadata", updateDebug);
  video.addEventListener("playing", () => {
    if (diagnostics.startupStage === "camera_starting") setStartupStage("camera_playing");
    maybeStartZxingFallback("video_playing_late_zxing");
  });
  const zxingScript = [...document.scripts].find((item) => item.src && item.src.includes("/static/vendor/zxing/"));
  zxingScript?.addEventListener("load", () => {
    diagnostics.zxingLoaded = zxingReady();
    updateDebug();
    maybeStartZxingFallback("zxing_script_load");
  });
  refreshDiagnosticButton.addEventListener("click", () => {
    renderDiagnostics();
    if (diagnostics.loopStartCount === 0) cameraStatus.textContent = "ZXing循环尚未启动";
  });
  copyDiagnosticButton.addEventListener("click", async () => {
    const snapshot = diagnosticsSnapshot();
    renderDiagnostics();
    try {
      await navigator.clipboard.writeText(JSON.stringify(snapshot, null, 2));
      cameraStatus.textContent = snapshot.loopStartCount === 0
        ? "安全诊断已复制；ZXing循环尚未启动"
        : "安全诊断已复制。";
    } catch (_) {
      cameraStatus.textContent = snapshot.loopStartCount === 0
        ? "无法自动复制；ZXing循环尚未启动"
        : "无法自动复制；请长按诊断文本手动复制。";
    }
  });
  window.addEventListener("pagehide", () => stopUnifiedScanner("", "pagehide"));

  resetFlowState();
  setNextBarVisible(false);
  updateCacheAvailability();
  refreshPendingCount().then(syncPending);
})();



