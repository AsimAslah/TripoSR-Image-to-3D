(() => {
  const form = document.getElementById("conversion-form");
  const imageInput = document.getElementById("image-input");
  const convertButton = document.getElementById("convert-button");
  const saveButton = document.getElementById("save-button");
  const progressPanel = document.getElementById("conversion-progress");
  const progressBar = document.getElementById("progress-bar");
  const progressStatus = document.getElementById("progress-status");
  const progressPercent = document.getElementById("progress-percent");
  const toastArea = document.getElementById("toast-area");
  const deviceSupport = document.getElementById("device-support");
  const activePreviews = new Map();
  let modelViewerPromise;

  const MB = 1024 * 1024;
  const PREVIEW_TIMEOUT_MS = 120000;

  function isIOS() {
    const ua = navigator.userAgent || "";
    return /iPad|iPhone|iPod/.test(ua)
      || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  }

  function isAndroid() {
    return /Android/i.test(navigator.userAgent || "");
  }

  function isMobile() {
    return isIOS() || isAndroid() || window.matchMedia("(max-width: 767px) and (pointer: coarse)").matches;
  }

  function isStandalone() {
    return window.matchMedia("(display-mode: standalone)").matches
      || window.navigator.standalone === true;
  }

  function platformName() {
    if (isIOS()) return "ios";
    if (isAndroid()) return "android";
    return "desktop";
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} bytes`;
    if (value < MB) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / MB).toFixed(1)} MB`;
  }

  function notify(message, type = "info") {
    if (!toastArea || !message) return;
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    toastArea.appendChild(toast);
    window.setTimeout(() => toast.remove(), 5200);
  }

  function setConversionProgress(status, percent = null) {
    progressStatus.textContent = status;
    if (Number.isFinite(percent)) {
      const measured = Math.max(0, Math.min(100, Math.round(percent)));
      progressBar.value = measured;
      progressBar.textContent = `${measured}%`;
      progressPercent.hidden = false;
      progressPercent.textContent = `${measured}%`;
    } else {
      progressBar.removeAttribute("value");
      progressBar.textContent = status;
      progressPercent.hidden = true;
    }
  }

  function setConversionLocked(locked) {
    imageInput.disabled = locked;
    convertButton.disabled = locked;
    document.querySelectorAll("#product-form input, #product-form textarea, #product-form select").forEach((field) => {
      field.disabled = locked;
    });
    document.querySelectorAll('[form="product-form"]').forEach((button) => {
      button.disabled = locked || !document.querySelector("input[name='conversion_id']");
    });
  }

  function updateDeviceSummary() {
    const standalone = isStandalone() ? "installed PWA" : "browser";
    if (isIOS()) {
      deviceSupport.textContent = `iPhone/iPad ${standalone} • Apple Quick Look uses validated USDZ`;
    } else if (isAndroid()) {
      deviceSupport.textContent = `Android ${standalone} • Scene Viewer uses validated GLB`;
    } else {
      deviceSupport.textContent = "Desktop 3D preview • use a supported phone for AR";
    }
    console.info("[Furniture AR] device", {
      platform: platformName(), standalone: isStandalone(), secureContext: window.isSecureContext,
    });
  }

  function hasWebGL() {
    try {
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("webgl2") || canvas.getContext("webgl");
      context?.getExtension("WEBGL_lose_context")?.loseContext();
      return Boolean(context);
    } catch (_error) {
      return false;
    }
  }

  function previewElements(root) {
    return {
      card: root.querySelector("[data-preview-card]"),
      heading: root.querySelector("[data-preview-heading]"),
      status: root.querySelector("[data-preview-status]"),
      message: root.querySelector("[data-preview-message]"),
      progressWrap: root.querySelector("[data-preview-progress]"),
      progress: root.querySelector("[data-preview-progress] progress"),
      surface: root.querySelector("[data-preview-surface]"),
      error: root.querySelector("[data-preview-error]"),
      errorMessage: root.querySelector("[data-preview-error-message]"),
      errorDownload: root.querySelector(".preview-error-download"),
      retry: root.querySelector(".retry-preview"),
      type: root.querySelector("[data-diagnostic-type]"),
      size: root.querySelector("[data-diagnostic-size]"),
      stage: root.querySelector("[data-diagnostic-stage]"),
      http: root.querySelector("[data-diagnostic-http]"),
      detail: root.querySelector("[data-diagnostic-detail]"),
    };
  }

  function setPreviewStage(root, stage, message, options = {}) {
    const elements = previewElements(root);
    if (stage !== "ready") elements.status.classList.remove("is-ready");
    elements.stage.textContent = stage;
    elements.message.textContent = message;
    elements.status.hidden = false;
    elements.error.hidden = true;
    if (options.loaded != null) {
      const total = options.total || 0;
      elements.progressWrap.hidden = false;
      if (total > 0) {
        elements.progress.value = Math.min(100, (options.loaded / total) * 100);
        elements.progress.textContent = `${Math.round((options.loaded / total) * 100)}%`;
      } else {
        elements.progress.removeAttribute("value");
      }
    } else {
      elements.progressWrap.hidden = true;
    }
  }

  function setTriggersBusy(root, busy) {
    root.querySelectorAll(".preview-trigger").forEach((button) => {
      button.disabled = busy;
      button.setAttribute("aria-busy", String(busy));
    });
  }

  async function fetchModel(url, declaredSize, signal, onProgress) {
    const response = await fetch(url, { cache: "no-store", credentials: "same-origin", signal });
    if (!response.ok) {
      const error = new Error(`The server returned HTTP ${response.status}.`);
      error.stage = "download";
      error.httpStatus = response.status;
      throw error;
    }
    const headerSize = Number(response.headers.get("content-length")) || 0;
    const total = headerSize || declaredSize || 0;
    if (!response.body?.getReader) {
      const blob = await response.blob();
      onProgress(blob.size, total || blob.size);
      return { blob, httpStatus: response.status };
    }
    const reader = response.body.getReader();
    const chunks = [];
    let loaded = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.byteLength;
      onProgress(loaded, total);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    return { blob: new Blob(chunks, { type: contentType }), httpStatus: response.status };
  }

  function unloadPreview(root, { hide = true } = {}) {
    const active = activePreviews.get(root);
    if (active) {
      active.controller?.abort();
      try { active.cleanup?.(); } catch (error) { console.warn("[Preview] cleanup failed", error); }
      if (active.objectUrl) URL.revokeObjectURL(active.objectUrl);
      activePreviews.delete(root);
    }
    const elements = previewElements(root);
    elements.surface.replaceChildren();
    elements.surface.hidden = true;
    elements.status.classList.remove("is-ready");
    elements.status.hidden = false;
    elements.error.hidden = true;
    elements.progressWrap.hidden = true;
    if (hide) elements.card.hidden = true;
    setTriggersBusy(root, false);
  }

  function sizeAllowsPreview(type, size) {
    if (!isMobile() || size < 10 * MB) return true;
    if (size > 25 * MB) {
      return window.confirm(
        `The ${type.toUpperCase()} file is ${formatBytes(size)}. Loading it may use significant mobile data and memory. Load it now?`,
      );
    }
    notify(`${type.toUpperCase()} is ${formatBytes(size)}; mobile loading may take longer.`, "info");
    return true;
  }

  async function ensureModelViewer() {
    if (!modelViewerPromise) {
      modelViewerPromise = import("https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js")
        .then(() => customElements.whenDefined("model-viewer"));
    }
    return modelViewerPromise;
  }

  async function mountGlb(root, objectUrl, active) {
    if (!hasWebGL()) {
      const error = new Error("WebGL is unavailable or disabled in this browser.");
      error.stage = "rendering";
      throw error;
    }
    setPreviewStage(root, "rendering", "Loading 3D viewer...");
    await ensureModelViewer();
    if (active.controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
    const elements = previewElements(root);
    const viewer = document.createElement("model-viewer");
    viewer.className = "lazy-model-viewer";
    viewer.setAttribute("alt", "Generated furniture 3D model");
    viewer.setAttribute("camera-controls", "");
    viewer.setAttribute("auto-rotate", "");
    viewer.setAttribute("shadow-intensity", "1");
    viewer.setAttribute("exposure", "1.05");
    viewer.setAttribute("field-of-view", "35deg");
    elements.surface.replaceChildren(viewer);
    elements.surface.hidden = false;
    active.cleanup = () => {
      viewer.pause?.();
      viewer.removeAttribute("src");
      viewer.remove();
    };
    await new Promise((resolve, reject) => {
      let settled = false;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        active.controller.signal.removeEventListener("abort", onAbort);
        callback(value);
      };
      const timeout = window.setTimeout(() => {
        const error = new Error("The viewer did not finish parsing the GLB within 45 seconds.");
        error.stage = "parsing";
        finish(reject, error);
      }, 45000);
      const onAbort = () => finish(reject, new DOMException("Preview loading was cancelled.", "AbortError"));
      active.controller.signal.addEventListener("abort", onAbort, { once: true });
      viewer.addEventListener("load", () => {
        setPreviewStage(root, "materials", "Applying materials...");
        requestAnimationFrame(() => finish(resolve));
      }, { once: true });
      viewer.addEventListener("error", (event) => {
        const error = new Error(event.detail?.message || "model-viewer could not parse or render this GLB.");
        error.stage = "parsing/rendering";
        finish(reject, error);
      }, { once: true });
      if (active.controller.signal.aborted) {
        onAbort();
        return;
      }
      viewer.src = objectUrl;
    });
  }

  async function mountObj(root, blob, active) {
    if (!hasWebGL()) {
      const error = new Error("WebGL is unavailable or disabled in this browser.");
      error.stage = "rendering";
      throw error;
    }
    setPreviewStage(root, "geometry", "Loading geometry...");
    const module = await import(`/static/obj-preview.js?v=${encodeURIComponent(window.APP_VERSION || "current")}`);
    if (active.controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
    const elements = previewElements(root);
    elements.surface.hidden = false;
    const controller = await module.createObjPreview(elements.surface, await blob.text(), (stage) => {
      if (stage === "materials") setPreviewStage(root, "materials", "Applying materials...");
    });
    active.cleanup = () => controller.dispose();
  }

  function showPreviewError(root, type, size, downloadUrl, error) {
    const elements = previewElements(root);
    const stage = error.stage || (error.name === "AbortError" ? "cancelled" : "unknown");
    const status = error.httpStatus || "No response";
    const userMessage = error.name === "AbortError"
      ? "Preview loading was cancelled or timed out. You can retry without converting again."
      : `${type.toUpperCase()} preview failed during ${stage}. You can retry or download the validated file.`;
    elements.status.hidden = true;
    elements.progressWrap.hidden = true;
    elements.surface.hidden = true;
    elements.error.hidden = false;
    elements.errorMessage.textContent = userMessage;
    elements.errorDownload.href = downloadUrl;
    elements.type.textContent = type.toUpperCase();
    elements.size.textContent = formatBytes(size);
    elements.stage.textContent = stage;
    elements.http.textContent = String(status);
    elements.detail.textContent = error.message || error.name || "Unknown preview error";
    console.error("[Preview] load failed", { type, size, stage, httpStatus: status, error });
    notify(`${type.toUpperCase()} preview could not be loaded.`, "error");
  }

  async function loadPreview(root, config) {
    const { type, url, downloadUrl, size } = config;
    const existing = activePreviews.get(root);
    if (existing?.type === type || !sizeAllowsPreview(type, size)) return;
    if (existing) unloadPreview(root, { hide: false });
    unloadPreview(root, { hide: false });
    const elements = previewElements(root);
    elements.card.hidden = false;
    elements.heading.textContent = `${type.toUpperCase()} Preview`;
    elements.type.textContent = type.toUpperCase();
    elements.size.textContent = formatBytes(size);
    elements.stage.textContent = "preparing";
    elements.http.textContent = "Pending";
    elements.detail.textContent = "None";
    elements.errorDownload.href = downloadUrl;
    elements.retry.dataset.previewType = type;
    elements.card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setTriggersBusy(root, true);
    setPreviewStage(root, "preparing", "Preparing preview...");

    const controller = new AbortController();
    const active = { ...config, controller, cleanup: null, objectUrl: null };
    activePreviews.set(root, active);
    const timeout = window.setTimeout(() => controller.abort(), PREVIEW_TIMEOUT_MS);
    try {
      const response = await fetchModel(url, size, controller.signal, (loaded, total) => {
        const copy = total ? `${formatBytes(loaded)} of ${formatBytes(total)}` : formatBytes(loaded);
        setPreviewStage(root, "download", `Downloading model: ${copy}`, { loaded, total });
      });
      elements.http.textContent = String(response.httpStatus);
      active.objectUrl = URL.createObjectURL(response.blob);
      if (type === "glb") await mountGlb(root, active.objectUrl, active);
      else await mountObj(root, response.blob, active);
      setPreviewStage(root, "ready", "Ready — drag to rotate, pinch or scroll to zoom.");
      elements.status.classList.add("is-ready");
      setTriggersBusy(root, false);
      console.info("[Preview] ready", { type, size, httpStatus: response.httpStatus });
    } catch (error) {
      if (activePreviews.get(root) !== active) return;
      active.cleanup?.();
      if (active.objectUrl) URL.revokeObjectURL(active.objectUrl);
      active.cleanup = null;
      active.objectUrl = null;
      activePreviews.delete(root);
      showPreviewError(root, type, size, downloadUrl, error);
      setTriggersBusy(root, false);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function initializeAR(root) {
    const card = root.querySelector(".ar-card");
    if (!card || card.dataset.initialized) return;
    card.dataset.initialized = "true";
    const status = card.querySelector(".ar-status");
    const iosAction = card.querySelector(".ios-ar-action");
    const androidAction = card.querySelector(".android-ar-action");
    const unavailable = card.querySelector(".ar-unavailable");
    const secure = window.isSecureContext || location.hostname === "localhost" || location.hostname === "127.0.0.1";

    if (isIOS()) {
      if (iosAction && card.dataset.usdzReady === "true" && secure) {
        iosAction.hidden = false;
        status.textContent = "Ready for Apple Quick Look using the validated USDZ file.";
        iosAction.addEventListener("click", () => {
          status.textContent = "Opening Apple Quick Look. Close it to return to this page.";
          sessionStorage.setItem("furniture-ar-launched", "true");
        });
      } else {
        unavailable.hidden = false;
        status.textContent = secure
          ? "iPhone AR file is not available for this model. GLB download and preview are still available."
          : "iPhone AR requires this page to be opened over HTTPS.";
      }
      return;
    }

    if (isAndroid()) {
      const modelUrl = androidAction?.dataset.modelUrl;
      if (androidAction && modelUrl && secure && new URL(modelUrl, location.href).protocol === "https:") {
        androidAction.hidden = false;
        status.textContent = "Ready for Android Scene Viewer. If unavailable, you will return to this page.";
        androidAction.addEventListener("click", () => {
          const file = encodeURIComponent(new URL(modelUrl, location.href).href);
          const fallback = encodeURIComponent(location.href);
          location.href = `intent://arvr.google.com/scene-viewer/1.0?file=${file}&mode=ar_preferred#Intent;scheme=https;package=com.google.ar.core;action=android.intent.action.VIEW;S.browser_fallback_url=${fallback};end;`;
          sessionStorage.setItem("furniture-ar-launched", "true");
        });
      } else {
        unavailable.hidden = false;
        status.textContent = secure ? "Android AR is unavailable for this model." : "Android AR requires HTTPS.";
      }
      return;
    }

    unavailable.hidden = false;
    status.textContent = "Desktop preview is available. AR controls appear only on supported phones.";
  }

  function initializeResult(root) {
    const previewRoot = root.matches?.("[data-preview-root]") ? root : root.querySelector?.("[data-preview-root]");
    if (!previewRoot || previewRoot.dataset.initialized) return;
    previewRoot.dataset.initialized = "true";
    initializeAR(previewRoot);
    previewRoot.querySelectorAll(".preview-trigger").forEach((button) => {
      button.addEventListener("click", () => loadPreview(previewRoot, {
        type: button.dataset.previewType,
        url: button.dataset.previewUrl,
        downloadUrl: button.dataset.downloadUrl,
        size: Number(button.dataset.fileSize) || 0,
      }));
    });
    previewRoot.querySelector(".unload-preview")?.addEventListener("click", () => unloadPreview(previewRoot));
    previewRoot.querySelector(".retry-preview")?.addEventListener("click", () => {
      const type = previewRoot.querySelector(".retry-preview").dataset.previewType;
      const button = previewRoot.querySelector(`.preview-trigger[data-preview-type="${type}"]`);
      if (button) loadPreview(previewRoot, {
        type, url: button.dataset.previewUrl, downloadUrl: button.dataset.downloadUrl,
        size: Number(button.dataset.fileSize) || 0,
      });
    });
  }

  function markReturnedFromAR() {
    if (sessionStorage.getItem("furniture-ar-launched") === "true") {
      document.querySelectorAll(".ar-status").forEach((status) => {
        status.textContent = "Returned to this product page from AR.";
      });
      sessionStorage.removeItem("furniture-ar-launched");
    }
  }

  updateDeviceSummary();
  initializeResult(document);
  window.addEventListener("pageshow", markReturnedFromAR);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") markReturnedFromAR();
  });

  imageInput.addEventListener("change", () => {
    if (imageInput.files.length) notify("Image ready for conversion.", "success");
  });

  form.addEventListener("htmx:beforeRequest", () => {
    setConversionLocked(true);
    progressPanel.hidden = false;
    setConversionProgress("Uploading image and starting conversion...");
    notify("Converting image to 3D...", "info");
  });

  form.addEventListener("htmx:xhr:progress", (event) => {
    const { loaded, total } = event.detail;
    if (total > 0 && loaded < total) setConversionProgress("Uploading image...", (loaded / total) * 100);
    else setConversionProgress("Generating geometry and model files...");
  });

  form.addEventListener("htmx:afterRequest", (event) => {
    setConversionLocked(false);
    setConversionProgress(
      event.detail.successful ? "3D model generated and validated" : "Conversion failed. See the error below.",
      event.detail.successful ? 100 : null,
    );
    notify(
      event.detail.successful ? "3D model generated successfully." : "3D conversion failed.",
      event.detail.successful ? "success" : "error",
    );
  });

  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.xhr.status >= 400) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
    const oldRoot = event.detail.target.querySelector?.("[data-preview-root]");
    if (oldRoot) unloadPreview(oldRoot);
  });

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.elt.closest("#product-form") || event.detail.elt.getAttribute("form") === "product-form") {
      document.querySelectorAll('[form="product-form"]').forEach((button) => { button.disabled = true; });
      if (saveButton) saveButton.textContent = "Saving...";
    }
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.elt.closest("#product-form") || event.detail.elt.getAttribute("form") === "product-form") {
      document.querySelectorAll('[form="product-form"]').forEach((button) => {
        button.disabled = event.detail.successful;
      });
      if (saveButton) saveButton.textContent = "Save to Supabase";
      notify(
        event.detail.successful ? "Saved to Supabase successfully." : "Supabase save failed.",
        event.detail.successful ? "success" : "error",
      );
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => initializeResult(event.detail.target));

  window.addEventListener("pagehide", () => {
    activePreviews.forEach((_value, root) => unloadPreview(root));
  });

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register(`/service-worker.js?v=${window.APP_VERSION || "current"}`, {
        scope: "/", updateViaCache: "none",
      }).then((registration) => registration.update()).catch((error) => {
        console.warn("[Furniture AR] service worker registration failed", error);
      });
    });
  }
})();
