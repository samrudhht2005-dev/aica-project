(function () {
    /**
     * AICA theme bootstrap — dark theme only.
     * Keeps data-theme + storage hooks for compatibility; no user-facing light mode.
     */
    var STORAGE_KEY = "theme";
    var FORCED = "dark";

    function applyDark() {
        document.documentElement.setAttribute("data-theme", FORCED);
        try {
            localStorage.setItem(STORAGE_KEY, FORCED);
        } catch (e) { /* ignore quota / private mode */ }
        try {
            document.dispatchEvent(new CustomEvent("aica:themechange", { detail: { theme: FORCED } }));
        } catch (e) { /* ignore */ }
    }

    applyDark();

    // Compatibility stub — always dark; ignores requests for light.
    window.AICA_applyTheme = function () {
        applyDark();
    };
})();
