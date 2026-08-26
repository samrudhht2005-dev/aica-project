/**
 * Shared download helper — browser uses <a download>; packaged AICA desktop
 * uses pywebview save_user_download (WebView2 often ignores blob downloads).
 */
(function (global) {
    function sanitizeFilename(name, fallback) {
        var raw = String(name || fallback || "aica_download.pdf").trim();
        raw = raw.replace(/[\\\/:*?"<>|]+/g, "_").replace(/\0/g, "");
        if (!raw) raw = fallback || "aica_download.pdf";
        if (raw.length > 180) raw = raw.slice(0, 180);
        if (!/\.[A-Za-z0-9]{1,8}$/.test(raw)) raw += ".pdf";
        return raw;
    }

    function blobToBase64(blob) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () {
                var result = String(reader.result || "");
                var idx = result.indexOf(",");
                resolve(idx >= 0 ? result.slice(idx + 1) : result);
            };
            reader.onerror = function () { reject(reader.error || new Error("read failed")); };
            reader.readAsDataURL(blob);
        });
    }

    function browserSaveBlob(blob, filename) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { try { URL.revokeObjectURL(url); } catch (e) {} }, 1500);
        return Promise.resolve({ ok: true, mode: "browser", filename: filename });
    }

    function hasDesktopSave() {
        try {
            return !!(
                global.AICA_DESKTOP
                && global.pywebview
                && global.pywebview.api
                && typeof global.pywebview.api.save_user_download === "function"
            );
        } catch (e) {
            return false;
        }
    }

    /**
     * @param {Blob} blob
     * @param {string} filename
     * @returns {Promise<{ok:boolean, cancelled?:boolean, filename?:string, folder?:string, error?:string, mode?:string}>}
     */
    async function downloadBlob(blob, filename) {
        var safe = sanitizeFilename(filename, "aica_download.pdf");
        if (!blob) {
            return { ok: false, error: "No file to download" };
        }
        if (hasDesktopSave()) {
            try {
                var b64 = await blobToBase64(blob);
                var result = await global.pywebview.api.save_user_download(safe, b64);
                if (result && result.ok) {
                    result.mode = "desktop";
                    return result;
                }
                if (result && result.cancelled) {
                    return { ok: false, cancelled: true, mode: "desktop" };
                }
                // Fall through to browser attempt if bridge failed unexpectedly
                if (result && result.error) {
                    console.warn("AICA desktop save failed:", result.error);
                }
            } catch (e) {
                console.warn("AICA desktop save error:", e);
            }
        }
        return browserSaveBlob(blob, safe);
    }

    /**
     * Fetch a same-origin PDF/CSV URL and save it (desktop-aware).
     */
    async function downloadUrl(url, filenameHint) {
        var res = await fetch(url, {
            credentials: "same-origin",
            headers: { Accept: "application/pdf,application/octet-stream,*/*" },
        });
        if (!res.ok) {
            throw new Error("Download failed (" + res.status + ")");
        }
        var ct = (res.headers.get("Content-Type") || "").toLowerCase();
        if (ct.includes("application/json") || ct.includes("text/html")) {
            throw new Error("Download failed — unexpected response");
        }
        var filename = filenameHint || "aica_download.pdf";
        var cd = res.headers.get("Content-Disposition") || "";
        var m = cd.match(/filename\*?=(?:UTF-8''|"?)([^";]+)"?/i);
        if (m && m[1]) {
            try { filename = decodeURIComponent(m[1].trim()); } catch (e) { filename = m[1].trim(); }
        }
        var blob = await res.blob();
        return downloadBlob(blob, filename);
    }

    global.AICA_downloadBlob = downloadBlob;
    global.AICA_downloadUrl = downloadUrl;
})(window);
