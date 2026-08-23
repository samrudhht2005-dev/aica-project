/**
 * AICA desktop auto-update UI (Phase 3–4: check + secure download).
 * Polls pywebview Python state only; never contacts GitHub from JS.
 */
(function () {
    "use strict";

    var POLL_MS = 2500;
    var DOWNLOAD_POLL_MS = 800;
    var POLL_MAX_IDLE = 120000;
    var pollTimer = null;
    var downloadPollTimer = null;
    var pollStartedAt = 0;
    var manualCheckActive = false;
    var downloadActive = false;

    function isDesktop() {
        return !!(window.AICA_DESKTOP || (window.pywebview && window.pywebview.api));
    }

    function hasUpdateApi() {
        return !!(window.pywebview && window.pywebview.api
            && typeof window.pywebview.api.get_update_status === "function");
    }

    function hasDownloadApi() {
        return !!(window.pywebview && window.pywebview.api
            && typeof window.pywebview.api.start_update_download === "function"
            && typeof window.pywebview.api.get_update_download_status === "function");
    }

    function hasApplyApi() {
        return !!(window.pywebview && window.pywebview.api
            && typeof window.pywebview.api.apply_staged_update === "function");
    }

    function t(key, fallback) {
        if (window.AICA_I18N && typeof window.AICA_I18N.t === "function") {
            return window.AICA_I18N.t(key, fallback);
        }
        return fallback != null ? fallback : key;
    }

    function escapeText(text) {
        var d = document.createElement("div");
        d.textContent = text == null ? "" : String(text);
        return d.textContent;
    }

    function formatBytes(n) {
        var v = Number(n) || 0;
        if (v <= 0) return "0 B";
        var units = ["B", "KB", "MB", "GB"];
        var i = 0;
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024;
            i += 1;
        }
        return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
    }

    function isTerminalStatus(status) {
        return [
            "up_to_date",
            "update_available",
            "no_network",
            "timeout",
            "invalid_manifest",
            "invalid_config",
            "older_manifest_ignored",
            "error"
        ].indexOf(status) >= 0;
    }

    function isDownloadTerminal(status) {
        return ["idle", "ready", "error", "cancelled"].indexOf(status) >= 0;
    }

    function dismissKey(version) {
        return "aica_update_dismissed_" + (version || "");
    }

    function isDismissed(version) {
        try {
            return sessionStorage.getItem(dismissKey(version)) === "1";
        } catch (e) {
            return false;
        }
    }

    function setDismissed(version) {
        try {
            sessionStorage.setItem(dismissKey(version), "1");
        } catch (e) { /* ignore */ }
    }

    function callStatus() {
        if (!hasUpdateApi()) return Promise.resolve(null);
        return Promise.resolve(window.pywebview.api.get_update_status())
            .catch(function () { return null; });
    }

    function callDownloadStatus() {
        if (!hasDownloadApi()) return Promise.resolve(null);
        return Promise.resolve(window.pywebview.api.get_update_download_status())
            .catch(function () { return null; });
    }

    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
        manualCheckActive = false;
    }

    function stopDownloadPolling() {
        if (downloadPollTimer) {
            clearInterval(downloadPollTimer);
            downloadPollTimer = null;
        }
        downloadActive = false;
    }

    function shouldKeepPolling(status) {
        if (!status) return manualCheckActive;
        if (status.checking) return true;
        if (status.status === "pending") return true;
        if (manualCheckActive && !isTerminalStatus(status.status)) return true;
        return false;
    }

    function shouldKeepDownloadPolling(dl) {
        if (!dl) return downloadActive;
        if (dl.active) return true;
        if (downloadActive && !isDownloadTerminal(dl.status)) return true;
        return false;
    }

    function startPolling() {
        if (pollTimer) return;
        pollStartedAt = Date.now();
        pollTimer = setInterval(function () {
            if (Date.now() - pollStartedAt > POLL_MAX_IDLE) {
                stopPolling();
                return;
            }
            refreshUi(false);
        }, POLL_MS);
    }

    function startDownloadPolling() {
        if (downloadPollTimer) return;
        downloadPollTimer = setInterval(function () {
            refreshDownloadUi().then(function (dl) {
                if (!shouldKeepDownloadPolling(dl)) {
                    stopDownloadPolling();
                }
            });
        }, DOWNLOAD_POLL_MS);
    }

    function profileEls() {
        return {
            current: document.getElementById("aicaProfileCurrentVersion"),
            latest: document.getElementById("aicaProfileLatestVersion"),
            latestRow: document.getElementById("aicaProfileLatestRow"),
            status: document.getElementById("aicaProfileUpdateStatus"),
            btn: document.getElementById("aicaCheckUpdatesBtn")
        };
    }

    function bannerEls() {
        return {
            root: document.getElementById("aicaUpdateBanner"),
            title: document.getElementById("aicaUpdateBannerTitle"),
            meta: document.getElementById("aicaUpdateBannerMeta"),
            notes: document.getElementById("aicaUpdateBannerNotes"),
            progressWrap: document.getElementById("aicaUpdateProgressWrap"),
            progressLabel: document.getElementById("aicaUpdateProgressLabel"),
            progressBar: document.getElementById("aicaUpdateProgressBar"),
            progressDetail: document.getElementById("aicaUpdateProgressDetail"),
            readyMsg: document.getElementById("aicaUpdateReadyMsg"),
            actions: document.getElementById("aicaUpdateBannerActions"),
            installMsg: document.getElementById("aicaUpdateInstallMsg"),
            btnLater: document.getElementById("aicaUpdateLaterBtn"),
            btnNow: document.getElementById("aicaUpdateNowBtn"),
            btnRetry: document.getElementById("aicaUpdateRetryBtn"),
            btnClose: document.getElementById("aicaUpdateCloseBtn"),
            btnRestart: document.getElementById("aicaUpdateRestartBtn")
        };
    }

    function setBannerActions(mode) {
        var b = bannerEls();
        if (b.btnLater) b.btnLater.hidden = mode !== "offer";
        if (b.btnNow) b.btnNow.hidden = mode !== "offer";
        if (b.btnRetry) b.btnRetry.hidden = mode !== "error";
        if (b.btnClose) b.btnClose.hidden = true;
        if (b.btnRestart) b.btnRestart.hidden = mode !== "ready";
    }

    function statusMessage(status) {
        if (!status) {
            return t("update.unable", "Unable to check for updates.");
        }
        if (status.checking || status.status === "checking" || status.status === "pending") {
            return t("update.checking", "Checking for updates…");
        }
        switch (status.status) {
            case "up_to_date":
                return t("update.upToDate", "You're up to date.");
            case "update_available":
                return t("update.available", "Update available.");
            case "invalid_manifest":
            case "invalid_config":
                return t("update.invalid", "Invalid update information.");
            case "no_network":
            case "timeout":
                return t("update.unable", "Unable to check for updates.");
            default:
                return t("update.unable", "Unable to check for updates.");
        }
    }

    function renderProfile(status) {
        var p = profileEls();
        if (!p.current) return;

        var installed = (status && status.installed_version) || window.AICA_VERSION || "—";
        p.current.textContent = "v" + escapeText(installed);

        if (p.latestRow && p.latest) {
            if (status && status.update_available && status.available_version) {
                p.latestRow.hidden = false;
                p.latest.textContent = "v" + escapeText(status.available_version);
            } else {
                p.latestRow.hidden = true;
            }
        }

        if (p.status) {
            p.status.textContent = statusMessage(status);
        }

        if (p.btn) {
            p.btn.disabled = !!(status && status.checking);
        }
    }

    function renderBanner(status, dl) {
        var b = bannerEls();
        if (!b.root) return;

        dl = dl || null;

        if (dl && (dl.status === "ready" || dl.active || dl.status === "error")) {
            b.root.hidden = false;
            if (b.notes) b.notes.hidden = true;
            if (b.meta) b.meta.hidden = true;
            if (b.installMsg) b.installMsg.hidden = true;

            if (dl.status === "ready") {
                if (b.title) b.title.textContent = t("update.downloadReady", "Update ready");
                if (b.progressWrap) b.progressWrap.hidden = true;
                if (b.readyMsg) {
                    b.readyMsg.hidden = false;
                    b.readyMsg.textContent = t(
                        "update.downloadReadyDetail",
                        "AICA v{version} has been downloaded and verified.\nRestart to apply the update."
                    ).replace("{version}", escapeText(dl.version || ""));
                }
                setBannerActions("ready");
                return;
            }

            if (dl.status === "error") {
                if (b.title) b.title.textContent = t("update.downloadFailed", "Download failed.");
                if (b.progressWrap) b.progressWrap.hidden = true;
                if (b.readyMsg) {
                    b.readyMsg.hidden = false;
                    b.readyMsg.textContent = dl.error
                        ? String(dl.error)
                        : t("update.downloadFailed", "Download failed.");
                }
                setBannerActions("error");
                return;
            }

            if (b.title) {
                b.title.textContent = dl.status === "verifying"
                    ? t("update.verifying", "Verifying update…")
                    : (dl.status === "starting"
                        ? t("update.preparing", "Preparing download…")
                        : t("update.downloading", "Downloading update…"));
            }
            if (b.readyMsg) b.readyMsg.hidden = true;
            if (b.progressWrap) b.progressWrap.hidden = false;
            if (b.progressLabel && dl.version) {
                b.progressLabel.textContent = "AICA v" + escapeText(dl.version);
            }
            if (b.progressBar) {
                if (dl.progress_percent == null || dl.status === "verifying") {
                    b.progressBar.style.width = "40%";
                    b.progressBar.classList.add("indeterminate");
                } else {
                    b.progressBar.classList.remove("indeterminate");
                    b.progressBar.style.width = Math.max(0, Math.min(100, dl.progress_percent)) + "%";
                }
            }
            if (b.progressDetail) {
                if (dl.status === "verifying") {
                    b.progressDetail.textContent = t("update.verifying", "Verifying update…");
                } else if (dl.total_bytes > 0) {
                    b.progressDetail.textContent = t(
                        "update.downloadProgress",
                        "Downloading {downloaded} of {total}"
                    )
                        .replace("{downloaded}", formatBytes(dl.bytes_downloaded))
                        .replace("{total}", formatBytes(dl.total_bytes));
                } else {
                    b.progressDetail.textContent = t("update.downloading", "Downloading update…");
                }
            }
            setBannerActions("none");
            if (b.btnLater) b.btnLater.hidden = true;
            if (b.btnNow) b.btnNow.hidden = true;
            if (b.btnRetry) b.btnRetry.hidden = true;
            if (b.btnClose) b.btnClose.hidden = true;
            return;
        }

        if (b.progressWrap) b.progressWrap.hidden = true;
        if (b.readyMsg) b.readyMsg.hidden = true;
        if (b.meta) b.meta.hidden = false;

        if (!status || !status.update_available || !status.available_version) {
            b.root.hidden = true;
            return;
        }

        if (isDismissed(status.available_version)) {
            b.root.hidden = true;
            return;
        }

        b.root.hidden = false;
        if (b.title) {
            b.title.textContent = t("update.bannerTitle", "Update available");
        }
        if (b.meta) {
            b.meta.textContent = t("update.bannerMeta", "AICA v{available} is available. You are using v{installed}.")
                .replace("{available}", escapeText(status.available_version))
                .replace("{installed}", escapeText(status.installed_version || ""));
        }
        if (b.notes) {
            b.notes.textContent = status.release_notes ? String(status.release_notes) : "";
            b.notes.hidden = !status.release_notes;
        }
        if (b.installMsg) b.installMsg.hidden = true;
        setBannerActions("offer");
    }

    function refreshDownloadUi() {
        return callDownloadStatus().then(function (dl) {
            return callStatus().then(function (status) {
                renderBanner(status, dl);
                return dl;
            });
        });
    }

    function refreshUi(fromManual) {
        return callDownloadStatus().then(function (dl) {
            return callStatus().then(function (status) {
                renderProfile(status);
                renderBanner(status, dl);

                if (shouldKeepDownloadPolling(dl)) {
                    startDownloadPolling();
                } else if (!downloadActive) {
                    stopDownloadPolling();
                }

                if (shouldKeepPolling(status)) {
                    startPolling();
                } else if (!fromManual) {
                    stopPolling();
                } else if (status && isTerminalStatus(status.status)) {
                    manualCheckActive = false;
                    stopPolling();
                }
                return status;
            });
        });
    }

    function onCheckNowClick(ev) {
        if (ev) ev.preventDefault();
        if (!hasUpdateApi()) return;

        var p = profileEls();
        if (p.status) {
            p.status.textContent = t("update.checking", "Checking for updates…");
        }
        if (p.btn) p.btn.disabled = true;

        manualCheckActive = true;
        startPolling();

        Promise.resolve(window.pywebview.api.check_for_updates_now())
            .then(function () { return refreshUi(true); })
            .catch(function () {
                if (p.status) {
                    p.status.textContent = t("update.unable", "Unable to check for updates.");
                }
                if (p.btn) p.btn.disabled = false;
                manualCheckActive = false;
            });
    }

    function onLaterClick(ev) {
        if (ev) ev.preventDefault();
        callStatus().then(function (status) {
            if (status && status.available_version) {
                setDismissed(status.available_version);
            }
            var b = bannerEls();
            if (b.root) b.root.hidden = true;
        });
    }

    function onUpdateNowClick(ev) {
        if (ev) ev.preventDefault();
        if (!hasDownloadApi()) return;

        downloadActive = true;
        startDownloadPolling();

        Promise.resolve(window.pywebview.api.start_update_download())
            .then(function () { return refreshDownloadUi(); })
            .then(function (dl) {
                if (shouldKeepDownloadPolling(dl)) {
                    startDownloadPolling();
                }
            })
            .catch(function () {
                downloadActive = false;
                stopDownloadPolling();
            });
    }

    function onRetryClick(ev) {
        if (ev) ev.preventDefault();
        onUpdateNowClick(ev);
    }

    function onRestartClick(ev) {
        if (ev) ev.preventDefault();
        if (!hasApplyApi()) return;

        var b = bannerEls();
        if (b.btnRestart) b.btnRestart.disabled = true;
        if (b.title) {
            b.title.textContent = t("update.preparingRestart", "Preparing to restart…");
        }
        if (b.readyMsg) {
            b.readyMsg.hidden = false;
            b.readyMsg.textContent = t(
                "update.preparingRestartDetail",
                "AICA will close so the update can be installed."
            );
        }
        if (b.btnRetry) b.btnRetry.hidden = true;
        if (b.btnRestart) b.btnRestart.hidden = true;

        Promise.resolve(window.pywebview.api.apply_staged_update())
            .then(function (result) {
                if (!result || !result.ok) {
                    if (b.title) {
                        b.title.textContent = t("update.applyFailed", "Unable to start the update.");
                    }
                    if (b.readyMsg) {
                        b.readyMsg.textContent = (result && result.error)
                            ? String(result.error)
                            : t("update.applyFailed", "Unable to start the update.");
                    }
                    if (b.btnRestart) {
                        b.btnRestart.disabled = false;
                        b.btnRestart.hidden = false;
                    }
                    if (b.btnRetry) {
                        b.btnRetry.hidden = false;
                        b.btnRetry.textContent = t("update.retry", "Retry");
                    }
                    setBannerActions("error");
                    if (b.btnRestart) b.btnRestart.hidden = true;
                    return;
                }
                // Updater launched — AICA will shut down shortly.
                if (b.title) {
                    b.title.textContent = t("update.restarting", "Restarting to update…");
                }
            })
            .catch(function () {
                if (b.title) {
                    b.title.textContent = t("update.applyFailed", "Unable to start the update.");
                }
                if (b.btnRestart) {
                    b.btnRestart.disabled = false;
                    b.btnRestart.hidden = false;
                }
            });
    }

    function onCloseClick(ev) {
        if (ev) ev.preventDefault();
        var b = bannerEls();
        if (b.root) b.root.hidden = true;
    }

    function bindEvents() {
        var p = profileEls();
        if (p.btn) p.btn.addEventListener("click", onCheckNowClick);
        var b = bannerEls();
        if (b.btnLater) b.btnLater.addEventListener("click", onLaterClick);
        if (b.btnNow) b.btnNow.addEventListener("click", onUpdateNowClick);
        if (b.btnRetry) b.btnRetry.addEventListener("click", onRetryClick);
        if (b.btnClose) b.btnClose.addEventListener("click", onCloseClick);
        if (b.btnRestart) b.btnRestart.addEventListener("click", onRestartClick);
    }

    function init() {
        if (!isDesktop() || !hasUpdateApi()) return;

        bindEvents();
        refreshUi(false).then(function (status) {
            if (shouldKeepPolling(status)) {
                startPolling();
            }
            return refreshDownloadUi();
        }).then(function (dl) {
            if (shouldKeepDownloadPolling(dl)) {
                startDownloadPolling();
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    window.AICA_UPDATE_UI = {
        refresh: function () { return refreshUi(false); },
        checkNow: onCheckNowClick,
        downloadNow: onUpdateNowClick,
        applyNow: onRestartClick
    };
})();
