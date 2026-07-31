(function cameraAdapterModule(global) {
  "use strict";

  const DEFAULT_DEVICE_KEY = "jba.camera.deviceId";
  const TORCH_UNSUPPORTED_MESSAGE = "当前镜头或浏览器不支持补光灯";
  const ANDROID_MAIN_CAMERA_LABEL = "camera 0, facing back";
  const START_TIMEOUT_MS = 10000;
  const MINIMAL_CONSTRAINTS = Object.freeze({
    audio: false,
    video: {facingMode: {ideal: "environment"}},
  });
  const scheduleTimeout = typeof global.setTimeout === "function"
    ? global.setTimeout.bind(global)
    : setTimeout;
  const cancelTimeout = typeof global.clearTimeout === "function"
    ? global.clearTimeout.bind(global)
    : clearTimeout;

  function firstDefined(values, fallback) {
    for (const value of values) {
      if (value !== undefined && value !== null) return value;
    }
    return fallback;
  }

  function platformInfo(options = {}) {
    const navigatorLike = options.navigator || global.navigator || {};
    const userAgent = String(firstDefined([options.userAgent, navigatorLike.userAgent], ""));
    const platform = String(firstDefined([options.platform, navigatorLike.platform], ""));
    const maxTouchPoints = Number(firstDefined([options.maxTouchPoints, navigatorLike.maxTouchPoints], 0));
    const iosDevice = /iPad|iPhone|iPod/i.test(userAgent)
      || (platform === "MacIntel" && maxTouchPoints > 1);
    const webkit = /AppleWebKit/i.test(userAgent) && !/Android/i.test(userAgent);
    if (iosDevice && webkit) return {kind: "ios-webkit", isIOSWebKit: true, isAndroid: false};
    if (/Android/i.test(userAgent)) return {kind: "android", isIOSWebKit: false, isAndroid: true};
    return {kind: "standard", isIOSWebKit: false, isAndroid: false};
  }

  function normalizedLabel(value) {
    return String(value || "").trim().toLocaleLowerCase();
  }

  function choosePreferredDevice(devices, rememberedDeviceId, platform) {
    const videoDevices = (devices || []).filter((device) => device.kind === "videoinput");
    if (rememberedDeviceId && videoDevices.some((device) => device.deviceId === rememberedDeviceId)) {
      return rememberedDeviceId;
    }
    if (platform && platform.isAndroid) {
      const androidMain = videoDevices.find(
        (device) => normalizedLabel(device.label) === ANDROID_MAIN_CAMERA_LABEL,
      );
      if (androidMain) return androidMain.deviceId;
    }
    return "";
  }

  function buildVideoConstraints(deviceId, platform) {
    const constraints = {
      width: {ideal: 1920},
      height: {ideal: 1080},
      frameRate: {ideal: 30, max: 30},
    };
    if (deviceId) {
      constraints.deviceId = {exact: deviceId};
    } else {
      constraints.facingMode = {ideal: "environment"};
    }
    if (!platform || !platform.isIOSWebKit) {
      constraints.resizeMode = {ideal: "none"};
      constraints.zoom = {ideal: 1};
    }
    return constraints;
  }

  function stopMediaStream(stream) {
    if (stream && stream.getTracks) stream.getTracks().forEach((track) => track.stop());
  }

  function timeoutError() {
    const error = new Error("摄像头启动超过 10 秒");
    error.name = "TimeoutError";
    return error;
  }

  function playbackError(error) {
    const wrapped = new Error((error && error.message) || "视频播放失败");
    wrapped.name = "PlaybackError";
    wrapped.cause = error;
    return wrapped;
  }

  function withDeadline(promise, deadline) {
    const remaining = Math.max(1, deadline - Date.now());
    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = scheduleTimeout(() => {
        settled = true;
        reject(timeoutError());
      }, remaining);
      Promise.resolve(promise).then((value) => {
        if (settled) {
          stopMediaStream(value);
          return;
        }
        settled = true;
        cancelTimeout(timer);
        resolve(value);
      }, (error) => {
        if (settled) return;
        settled = true;
        cancelTimeout(timer);
        reject(error);
      });
    });
  }

  function settleOptional(promise, milliseconds = 1000) {
    return Promise.race([
      Promise.resolve(promise).then(() => true, () => false),
      new Promise((resolve) => scheduleTimeout(() => resolve(false), milliseconds)),
    ]);
  }

  function cameraErrorDetails(error, options = {}) {
    const name = (error && error.name) || "Error";
    const messages = {
      SecurityError: "摄像头必须通过 HTTPS 使用；请切换到安全地址。",
      NotAllowedError: "未获得摄像头权限；请在 Safari/浏览器设置中允许相机。",
      NotFoundError: "没有检测到可用摄像头。",
      DevicesNotFoundError: "没有检测到可用摄像头。",
      NotReadableError: "摄像头可能被其他应用占用，请关闭占用后重试。",
      TrackStartError: "摄像头可能被其他应用占用，请关闭占用后重试。",
      AbortError: "摄像头启动被系统中止，可能正被其他应用占用。",
      NotSupportedError: "当前浏览器不支持摄像头。",
      TypeError: "当前浏览器不支持所需摄像头接口。",
      PlaybackError: "摄像头已授权，但视频播放失败。",
      TimeoutError: "摄像头启动超时（10 秒），请重试或切换镜头。",
      OverconstrainedError: "保存的镜头已失效，自动降级仍失败。",
    };
    const rawMessage = String((error && error.message) || "").replace(/[\r\n\t]+/g, " ").slice(0, 180);
    return {
      message: messages[name] || "无法启动摄像头，请根据诊断信息重试。",
      diagnostic: {
        errorName: name,
        errorMessage: rawMessage,
        secureContext: Boolean(firstDefined([options.secureContext, global.isSecureContext], false)),
        protocol: String((global.location && global.location.protocol) || "unknown"),
        platform: (options.platform && options.platform.kind) || "unknown",
        mediaDevices: Boolean(options.mediaDevices),
        getUserMedia: Boolean(options.mediaDevices && options.mediaDevices.getUserMedia),
        at: new Date().toISOString(),
      },
    };
  }

  function mapRoiToVideo(roiRect, elementRect, videoWidth, videoHeight, objectFit = "contain") {
    if (!videoWidth || !videoHeight || !elementRect.width || !elementRect.height) return null;
    const scale = objectFit === "cover"
      ? Math.max(elementRect.width / videoWidth, elementRect.height / videoHeight)
      : Math.min(elementRect.width / videoWidth, elementRect.height / videoHeight);
    const renderedWidth = videoWidth * scale;
    const renderedHeight = videoHeight * scale;
    const offsetX = elementRect.left + (elementRect.width - renderedWidth) / 2;
    const offsetY = elementRect.top + (elementRect.height - renderedHeight) / 2;
    const left = Math.max(0, (roiRect.left - offsetX) / scale);
    const top = Math.max(0, (roiRect.top - offsetY) / scale);
    const right = Math.min(videoWidth, (roiRect.right - offsetX) / scale);
    const bottom = Math.min(videoHeight, (roiRect.bottom - offsetY) / scale);
    if (right <= left || bottom <= top) return null;
    return {
      x: Math.round(left),
      y: Math.round(top),
      width: Math.max(1, Math.round(right - left)),
      height: Math.max(1, Math.round(bottom - top)),
    };
  }

  async function createBarcodeDetector() {
    if (!global.BarcodeDetector) return null;
    const supported = await global.BarcodeDetector.getSupportedFormats();
    const formats = ["ean_13", "ean_8", "upc_a", "upc_e"]
      .filter((format) => supported.includes(format));
    return formats.length ? new global.BarcodeDetector({formats}) : null;
  }

  function isVideoFrameReady(video) {
    return Boolean(
      video
      && video.readyState >= 2
      && video.videoWidth > 0
      && video.videoHeight > 0
    );
  }

  async function detectBarcode(detector, video) {
    if (!detector || !isVideoFrameReady(video)) return [];
    return detector.detect(video);
  }

  function defaultValidJan(value) {
    const digits = String(value || "").replace(/\D+/g, "");
    if (![8, 12, 13].includes(digits.length)) return null;
    const body = digits.slice(0, -1);
    const sum = [...body].reduce((total, digit, index) => (
      total + Number(digit) * (((body.length - index) % 2 === 0) ? 1 : 3)
    ), 0);
    if ((10 - (sum % 10)) % 10 !== Number(digits[digits.length - 1])) return null;
    return digits.length === 12 ? `0${digits}` : digits;
  }

  class UnifiedJanScanner {
    constructor(options = {}) {
      this.video = options.video;
      this.placeholder = options.placeholder || null;
      this.status = options.status || null;
      this.cameraSelect = options.cameraSelect || null;
      this.torchButton = options.torchButton || null;
      this.zoomOut = options.zoomOut || null;
      this.zoomReset = options.zoomReset || null;
      this.zoomIn = options.zoomIn || null;
      this.debug = options.debug || null;
      this.onCode = options.onCode || (() => {});
      this.validJan = options.validJan || defaultValidJan;
      this.scanIntervalMs = Number(options.scanIntervalMs || 180);
      this.sameCodeDebounceMs = Number(options.sameCodeDebounceMs || 1600);
      this.noResultFallbackMs = Number(options.noResultFallbackMs || 4000);
      this.cameraAdapter = options.cameraAdapter || new CameraAdapter(options.cameraOptions || {});
      this.detector = null;
      this.zxingReader = null;
      this.scanTimer = null;
      this.scanFrame = null;
      this.scanning = false;
      this.track = null;
      this.stream = null;
      this.capabilities = {};
      this.zoomValue = 1;
      this.torchOn = false;
      this.lastCode = "";
      this.lastCodeAt = 0;
      this.loopStartedAt = 0;
      this.loopGeneration = 0;
      this.stats = {
        zxingLoaded: false,
        zxingReaderCreated: false,
        loopRunning: false,
        decodeAttempts: 0,
        framesTotal: 0,
        decodesPerSecond: 0,
        secondStartedAt: Date.now(),
        secondAttempts: 0,
        mode: "idle",
        roiMode: "full_frame",
        lastException: "",
        lastStopReason: "",
      };
    }

    setStatus(message) {
      if (this.status && message) this.status.textContent = message;
    }

    noteAttempt(mode) {
      this.stats.mode = mode;
      if (this.loopStartedAt && Date.now() - this.loopStartedAt >= this.noResultFallbackMs) {
        this.stats.roiMode = "full_frame_fallback";
      }
      this.stats.loopRunning = true;
      this.stats.decodeAttempts += 1;
      this.stats.framesTotal += 1;
      this.stats.secondAttempts += 1;
      const now = Date.now();
      if (now - this.stats.secondStartedAt >= 1000) {
        this.stats.decodesPerSecond = this.stats.secondAttempts;
        this.stats.secondAttempts = 0;
        this.stats.secondStartedAt = now;
      }
      this.renderDiagnostics();
    }

    rememberException(error) {
      const name = (error && (error.name || (error.constructor && error.constructor.name))) || "";
      if (name) this.stats.lastException = name;
      this.renderDiagnostics();
    }

    isZxingReady() {
      return Boolean(
        global.ZXingBrowser
        && global.ZXingBrowser.BrowserMultiFormatReader
        && global.ZXingBrowser.BarcodeFormat
      );
    }

    waitForVideoFrame(timeoutMs = 5000) {
      if (isVideoFrameReady(this.video)) return Promise.resolve();
      return new Promise((resolve, reject) => {
        let settled = false;
        const cleanup = () => {
          this.video.removeEventListener("loadedmetadata", check);
          this.video.removeEventListener("playing", check);
          this.video.removeEventListener("canplay", check);
        };
        const finish = (fn, value) => {
          if (settled) return;
          settled = true;
          cleanup();
          fn(value);
        };
        const timer = scheduleTimeout(() => {
          const error = new Error(`empty video frame ${this.video.readyState}/${this.video.videoWidth}x${this.video.videoHeight}`);
          error.name = "EmptyVideoFrame";
          finish(reject, error);
        }, timeoutMs);
        const check = () => {
          if (!isVideoFrameReady(this.video)) return;
          cancelTimeout(timer);
          finish(resolve);
        };
        this.video.addEventListener("loadedmetadata", check);
        this.video.addEventListener("playing", check);
        this.video.addEventListener("canplay", check);
        check();
      });
    }

    acceptCode(raw) {
      const value = this.validJan(raw);
      if (!value) return false;
      const now = Date.now();
      if (value === this.lastCode && now - this.lastCodeAt < this.sameCodeDebounceMs) return false;
      this.lastCode = value;
      this.lastCodeAt = now;
      this.onCode(value);
      return true;
    }

    async startBarcodeDetectorLoop(loopId) {
      if (!this.detector) return false;
      await this.waitForVideoFrame();
      this.scanning = true;
      const decode = async () => {
        this.scanFrame = null;
        if (!this.scanning || loopId !== this.loopGeneration || !this.detector) return;
        if (!isVideoFrameReady(this.video)) {
          this.rememberException({name: `EmptyVideoFrame:${this.video.readyState}/${this.video.videoWidth}x${this.video.videoHeight}`});
        } else {
          try {
            this.noteAttempt("BarcodeDetector full frame");
            const codes = await detectBarcode(this.detector, this.video);
            const value = codes.map((code) => code.rawValue).find((item) => this.validJan(item));
            if (value && this.acceptCode(value)) return;
          } catch (error) {
            this.rememberException(error);
          }
        }
        if (this.scanning && loopId === this.loopGeneration) {
          this.scanFrame = global.requestAnimationFrame ? global.requestAnimationFrame(decode) : scheduleTimeout(decode, this.scanIntervalMs);
        }
      };
      decode();
      return true;
    }

    async startZxingLoop(loopId) {
      const Reader = global.ZXingBrowser && global.ZXingBrowser.BrowserMultiFormatReader;
      const formats = global.ZXingBrowser && global.ZXingBrowser.BarcodeFormat;
      if (!Reader || !formats) throw new Error("ZXingUnavailable");
      this.zxingReader = new Reader(undefined, {
        delayBetweenScanAttempts: this.scanIntervalMs,
        delayBetweenScanSuccess: this.sameCodeDebounceMs,
      });
      this.zxingReader.possibleFormats = [formats.EAN_13, formats.EAN_8, formats.UPC_A];
      this.stats.zxingLoaded = true;
      this.stats.zxingReaderCreated = true;
      this.stats.roiMode = "full_frame";
      await this.waitForVideoFrame();
      this.scanning = true;
      const decode = () => {
        if (!this.scanning || loopId !== this.loopGeneration || !this.zxingReader) return;
        if (!isVideoFrameReady(this.video)) {
          this.rememberException({name: `EmptyVideoFrame:${this.video.readyState}/${this.video.videoWidth}x${this.video.videoHeight}`});
          this.scanTimer = scheduleTimeout(decode, this.scanIntervalMs);
          return;
        }
        try {
          this.noteAttempt("ZXing scanning");
          const result = this.zxingReader.decode(this.video);
          const value = result && (typeof result.getText === "function" ? result.getText() : result.text);
          if (this.acceptCode(value)) return;
        } catch (error) {
          this.rememberException(error);
        }
        this.scanTimer = scheduleTimeout(decode, this.scanIntervalMs);
      };
      decode();
      this.setStatus("ZXing loaded；decode loop running。");
    }

    populateDevices(devices) {
      if (!this.cameraSelect) return;
      this.cameraSelect.replaceChildren();
      devices.forEach((device, index) => {
        const option = global.document.createElement("option");
        option.value = device.deviceId;
        option.textContent = device.label || `摄像头 ${index + 1}`;
        this.cameraSelect.append(option);
      });
      this.cameraSelect.hidden = devices.length < 2;
      const settings = (this.track && this.track.getSettings && this.track.getSettings()) || {};
      this.cameraSelect.value = settings.deviceId || "";
    }

    async start(deviceId = "") {
      this.stop("restart");
      this.setStatus("正在请求后置摄像头…");
      const state = await this.cameraAdapter.start(deviceId, {onStream: async (stream) => {
        this.stream = stream;
        this.track = stream.getVideoTracks()[0];
        this.video.srcObject = stream;
        await this.video.play();
        await this.waitForVideoFrame();
        this.video.classList.add("active");
        if (this.placeholder) this.placeholder.hidden = true;
      }});
      this.stream = state.stream;
      this.track = state.track;
      this.capabilities = state.capabilities || {};
      this.zoomValue = state.settings.zoom === undefined || state.settings.zoom === null ? 1 : state.settings.zoom;
      this.populateDevices(state.devices || []);
      if (this.torchButton) {
        this.torchButton.hidden = !state.torchSupported;
        this.torchButton.disabled = !state.torchSupported;
      }
      const zoomAvailable = Boolean(this.capabilities.zoom);
      if (this.zoomOut) this.zoomOut.hidden = !zoomAvailable;
      if (this.zoomReset) {
        this.zoomReset.hidden = !zoomAvailable;
        this.zoomReset.textContent = `${Number(this.zoomValue || 1).toFixed(1)}×`;
      }
      if (this.zoomIn) this.zoomIn.hidden = !zoomAvailable;
      this.stats.zxingLoaded = this.isZxingReady();
      try {
        this.detector = await createBarcodeDetector();
      } catch (error) {
        this.detector = null;
        state.warnings.push({stage: "BarcodeDetector", name: (error && error.name) || "Error"});
      }
      const loopId = ++this.loopGeneration;
      this.loopStartedAt = Date.now();
      this.stats.roiMode = "full_frame";
      if (await this.startBarcodeDetectorLoop(loopId)) {
        this.setStatus("请将 JAN 条码完整放入画面。");
      } else {
        await this.startZxingLoop(loopId);
      }
      this.renderDiagnostics();
      return state;
    }

    pauseAfterSuccess(durationMs = 950) {
      this.scanning = false;
      if (this.scanTimer) cancelTimeout(this.scanTimer);
      if (this.scanFrame) {
        if (global.cancelAnimationFrame) global.cancelAnimationFrame(this.scanFrame);
        else cancelTimeout(this.scanFrame);
      }
      this.scanTimer = null;
      this.scanFrame = null;
      this.stats.loopRunning = false;
      this.stats.lastStopReason = `scan_success_pause_${Math.round(durationMs)}ms`;
      this.renderDiagnostics();
    }

    stop(reason = "stop") {
      this.scanning = false;
      this.loopGeneration += 1;
      if (this.scanTimer) cancelTimeout(this.scanTimer);
      if (this.scanFrame) {
        if (global.cancelAnimationFrame) global.cancelAnimationFrame(this.scanFrame);
        else cancelTimeout(this.scanFrame);
      }
      this.scanTimer = null;
      this.scanFrame = null;
      if (this.zxingReader && this.zxingReader.reset) this.zxingReader.reset();
      this.zxingReader = null;
      this.detector = null;
      this.cameraAdapter.stop();
      this.stream = null;
      this.track = null;
      this.capabilities = {};
      this.torchOn = false;
      this.stats.loopRunning = false;
      this.stats.lastStopReason = reason;
      if (this.video) {
        this.video.srcObject = null;
        this.video.classList.remove("active");
      }
      if (this.placeholder) this.placeholder.hidden = false;
      if (this.torchButton) {
        this.torchButton.hidden = true;
        this.torchButton.disabled = true;
        this.torchButton.textContent = "补光灯";
      }
      this.renderDiagnostics();
    }

    async switchDevice(deviceId) {
      return this.start(deviceId);
    }

    async setTorch(enabled) {
      const result = await this.cameraAdapter.setTorch(enabled);
      this.torchOn = result.enabled;
      if (this.torchButton) {
        this.torchButton.textContent = this.torchOn ? "关闭补光" : "补光灯";
        if (!result.supported) {
          this.torchButton.hidden = true;
          this.torchButton.disabled = true;
          if (result.shouldNotify) this.setStatus(result.message);
        }
      }
      return result;
    }

    async setZoom(value) {
      const applied = await this.cameraAdapter.setZoom(value);
      if (applied !== null) {
        this.zoomValue = applied;
        if (this.zoomReset) this.zoomReset.textContent = `${this.zoomValue.toFixed(1)}×`;
      }
      this.renderDiagnostics();
      return applied;
    }

    async focusAt(point) {
      return this.cameraAdapter.focusAt(point);
    }

    diagnostics() {
      const settings = (this.track && this.track.getSettings && this.track.getSettings()) || {};
      const selected = this.cameraSelect && this.cameraSelect.selectedOptions && this.cameraSelect.selectedOptions[0];
      return {
        zxing: this.stats.zxingLoaded ? "ZXing loaded" : "ZXing missing",
        zxingReaderCreated: this.stats.zxingReaderCreated,
        decodeLoop: this.stats.loopRunning ? "decode loop running" : "decode loop stopped",
        decodesPerSecond: this.stats.decodesPerSecond,
        framesTotal: this.stats.framesTotal,
        roiMode: this.stats.roiMode,
        readyState: this.video && this.video.readyState,
        videoWidth: this.video && this.video.videoWidth,
        videoHeight: this.video && this.video.videoHeight,
        lastException: this.stats.lastException || "none",
        lastStopReason: this.stats.lastStopReason || "none",
        mode: this.stats.mode,
        deviceId: settings.deviceId,
        label: (selected && selected.textContent) || (this.track && this.track.label) || "",
        settingsResolution: `${settings.width || "?"}×${settings.height || "?"}`,
        zoom: settings.zoom === undefined || settings.zoom === null ? 1 : settings.zoom,
        focusMode: settings.focusMode || "unsupported",
      };
    }

    renderDiagnostics() {
      if (!this.debug) return;
      this.debug.hidden = false;
      this.debug.textContent = JSON.stringify(this.diagnostics(), null, 2);
    }
  }

  class CameraAdapter {
    constructor(options = {}) {
      const navigatorLike = options.navigator || global.navigator || {};
      this.mediaDevices = options.mediaDevices || navigatorLike.mediaDevices;
      this.storage = options.storage || global.localStorage;
      this.deviceKey = options.deviceKey || DEFAULT_DEVICE_KEY;
      this.platform = options.platformInfo || platformInfo({
        navigator: navigatorLike,
        userAgent: options.userAgent,
        platform: options.platform,
        maxTouchPoints: options.maxTouchPoints,
      });
      this.secureContext = firstDefined(
        [options.secureContext, global.isSecureContext],
        true,
      );
      this.stream = null;
      this.track = null;
      this.devices = [];
      this.capabilities = {};
      this.torchOn = false;
      this.torchFailed = false;
      this.torchFailureNotified = false;
      this.startTimeoutMs = Number(options.startTimeoutMs || START_TIMEOUT_MS);
      this.warnings = [];
    }

    rememberedDeviceId() {
      try {
        return (this.storage && this.storage.getItem(this.deviceKey)) || "";
      } catch (_) {
        return "";
      }
    }

    rememberDeviceId(deviceId) {
      if (!deviceId) return;
      try {
        if (this.storage) this.storage.setItem(this.deviceKey, deviceId);
      } catch (_) {}
    }

    forgetDeviceId() {
      try {
        if (this.storage) this.storage.removeItem(this.deviceKey);
      } catch (_) {}
    }

    async enumerate() {
      if (!this.mediaDevices || !this.mediaDevices.enumerateDevices) return [];
      try {
        this.devices = (await this.mediaDevices.enumerateDevices())
          .filter((device) => device.kind === "videoinput");
      } catch (error) {
        this.devices = [];
        this.warnings.push({stage: "enumerateDevices", name: error && error.name || "Error"});
      }
      return this.devices;
    }

    stop() {
      if (this.stream && this.stream.getTracks) {
        this.stream.getTracks().forEach((track) => track.stop());
      } else {
        if (this.track && this.track.stop) this.track.stop();
      }
      this.stream = null;
      this.track = null;
      this.capabilities = {};
      this.torchOn = false;
      this.torchFailed = false;
      this.torchFailureNotified = false;
      this.warnings = [];
    }

    async openStream(deviceId) {
      return this.mediaDevices.getUserMedia({
        video: buildVideoConstraints(deviceId, this.platform),
        audio: false,
      });
    }

    async playStream(onStream, deadline) {
      if (!onStream) return;
      try {
        await withDeadline(Promise.resolve(onStream(this.stream)), deadline);
      } catch (error) {
        throw playbackError(error);
      }
    }

    attachStream(stream) {
      this.stream = stream;
      this.track = stream && stream.getVideoTracks ? stream.getVideoTracks()[0] : null;
      if (!this.track) {
        const error = new Error("摄像头没有返回视频轨道");
        error.name = "NotFoundError";
        throw error;
      }
    }

    async requestFallbackStream(deadline) {
      try {
        return await withDeadline(
          this.mediaDevices.getUserMedia(MINIMAL_CONSTRAINTS), deadline,
        );
      } catch (environmentError) {
        this.warnings.push({stage: "environmentFallback", name: environmentError && environmentError.name || "Error"});
        return withDeadline(
          this.mediaDevices.getUserMedia({audio: false, video: true}), deadline,
        );
      }
    }

    async start(requestedDeviceId = "", options = {}) {
      if (!this.secureContext) {
        const error = new Error("摄像头必须通过 HTTPS 使用");
        error.name = "SecurityError";
        throw error;
      }
      if (!this.mediaDevices || !this.mediaDevices.getUserMedia) {
        const error = new Error("当前浏览器不支持摄像头");
        error.name = "NotSupportedError";
        throw error;
      }

      this.stop();
      const deadline = Date.now() + Math.max(1, Number(options.timeoutMs || this.startTimeoutMs));
      // This call intentionally happens before the first await so iPhone Safari
      // receives getUserMedia directly in the button-click call stack.
      const initialRequest = this.mediaDevices.getUserMedia(MINIMAL_CONSTRAINTS);
      try {
        this.attachStream(await withDeadline(initialRequest, deadline));
      } catch (error) {
        if (!["OverconstrainedError", "TypeError"].includes(error && error.name)) throw error;
        this.attachStream(await withDeadline(
          this.mediaDevices.getUserMedia({audio: false, video: true}), deadline,
        ));
      }
      await this.playStream(options.onStream, deadline);

      const afterPermission = await withDeadline(this.enumerate(), deadline);
      const remembered = this.platform.isAndroid ? this.rememberedDeviceId() : "";
      if (remembered && !afterPermission.some((device) => device.deviceId === remembered)) {
        this.forgetDeviceId();
      }
      const desiredDeviceId = requestedDeviceId
        || (this.platform.isAndroid
          ? choosePreferredDevice(afterPermission, remembered, this.platform)
          : "");
      const currentDeviceId = (
        this.track && this.track.getSettings ? this.track.getSettings().deviceId : ""
      ) || "";
      if (desiredDeviceId && desiredDeviceId !== currentDeviceId) {
        stopMediaStream(this.stream);
        this.stream = null;
        this.track = null;
        try {
          this.attachStream(await withDeadline(this.openStream(desiredDeviceId), deadline));
        } catch (error) {
          this.warnings.push({stage: "preferredDevice", name: error && error.name || "Error"});
          this.forgetDeviceId();
          this.attachStream(await this.requestFallbackStream(deadline));
        }
        await this.playStream(options.onStream, deadline);
      }

      const preferencesApplied = await settleOptional(this.applyPreferences());
      if (!preferencesApplied) this.warnings.push({stage: "applyPreferences", name: "OptionalTimeout"});
      const activeDeviceId = (
        this.track && this.track.getSettings
          ? this.track.getSettings().deviceId
          : ""
      ) || requestedDeviceId;
      this.rememberDeviceId(activeDeviceId);
      return this.snapshot();
    }

    refreshCapabilities() {
      try {
        this.capabilities = (
          this.track && this.track.getCapabilities
            ? this.track.getCapabilities()
            : {}
        ) || {};
      } catch (_) {
        this.capabilities = {};
      }
      this.torchOn = false;
      this.torchFailed = false;
      this.torchFailureNotified = false;
      return this.capabilities;
    }

    async applyPreferences() {
      const capabilities = this.refreshCapabilities();
      const advanced = {};
      if (Array.isArray(capabilities.focusMode) && capabilities.focusMode.includes("continuous")) {
        advanced.focusMode = "continuous";
      }
      if (
        capabilities.zoom
        && capabilities.zoom.min <= 1
        && capabilities.zoom.max >= 1
      ) {
        advanced.zoom = 1;
      }
      if (
        Array.isArray(capabilities.resizeMode)
        && capabilities.resizeMode.includes("none")
      ) {
        advanced.resizeMode = "none";
      }
      if (Object.keys(advanced).length) {
        try {
          await this.track.applyConstraints({advanced: [advanced]});
        } catch (error) {
          this.warnings.push({stage: "applyPreferences", name: error && error.name || "Error"});
        }
      }
      return capabilities;
    }

    torchSupported() {
      return this.capabilities && this.capabilities.torch === true && !this.torchFailed;
    }

    async setTorch(enabled) {
      if (!this.track || !this.torchSupported()) {
        return {
          supported: false,
          enabled: false,
          message: TORCH_UNSUPPORTED_MESSAGE,
          shouldNotify: false,
        };
      }
      try {
        await this.track.applyConstraints({advanced: [{torch: Boolean(enabled)}]});
        this.torchOn = Boolean(enabled);
        return {supported: true, enabled: this.torchOn, message: "", shouldNotify: false};
      } catch (error) {
        this.torchOn = false;
        this.torchFailed = true;
        const shouldNotify = !this.torchFailureNotified;
        this.torchFailureNotified = true;
        return {
          supported: false,
          enabled: false,
          errorName: (error && error.name) || "WebKitError",
          message: TORCH_UNSUPPORTED_MESSAGE,
          shouldNotify,
        };
      }
    }

    async setZoom(value) {
      const zoom = this.capabilities && this.capabilities.zoom;
      if (!this.track || !zoom) return null;
      const next = Math.min(zoom.max, Math.max(zoom.min, value));
      try {
        await this.track.applyConstraints({advanced: [{zoom: next}]});
        return next;
      } catch (error) {
        this.warnings.push({stage: "zoom", name: error && error.name || "Error"});
        return null;
      }
    }

    async focusAt(point) {
      const focusModes = this.capabilities && this.capabilities.focusMode;
      if (!this.track || !Array.isArray(focusModes)) return false;
      const focusMode = focusModes.includes("single-shot")
        ? "single-shot"
        : (focusModes.includes("continuous") ? "continuous" : null);
      if (!focusMode) return false;
      try {
        await this.track.applyConstraints({
          advanced: [{focusMode, pointsOfInterest: [point]}],
        });
        return true;
      } catch (error) {
        this.warnings.push({stage: "focus", name: error && error.name || "Error"});
        return false;
      }
    }

    snapshot() {
      const settings = (
        this.track && this.track.getSettings ? this.track.getSettings() : {}
      ) || {};
      return {
        stream: this.stream,
        track: this.track,
        devices: this.devices,
        capabilities: this.capabilities,
        settings,
        platform: this.platform,
        torchSupported: this.torchSupported(),
        warnings: [...this.warnings],
      };
    }
  }

  const api = Object.freeze({
    ANDROID_MAIN_CAMERA_LABEL,
    CameraAdapter,
    UnifiedJanScanner,
    DEFAULT_DEVICE_KEY,
    MINIMAL_CONSTRAINTS,
    START_TIMEOUT_MS,
    TORCH_UNSUPPORTED_MESSAGE,
    buildVideoConstraints,
    choosePreferredDevice,
    cameraErrorDetails,
    createBarcodeDetector,
    detectBarcode,
    isVideoFrameReady,
    mapRoiToVideo,
    platformInfo,
  });
  global.JBACamera = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
