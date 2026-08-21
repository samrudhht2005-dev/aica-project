(function () {
    const STORAGE_KEY = "aica_lang";
    const supported = ["en", "kn", "hi"];
    let dict = {};
    let phrases = {};
    let lang = "en";

    function nestedGet(obj, path) {
        return path.split(".").reduce((o, k) => (o && o[k] != null ? o[k] : null), obj);
    }

    function t(key, fallback) {
        const v = nestedGet(dict, key);
        return v != null ? String(v) : (fallback != null ? fallback : key);
    }

    function setTextPreservingIcons(el, text) {
        const explicit = el.querySelector(":scope > [data-i18n-target]");
        if (explicit) {
            explicit.textContent = text;
            return;
        }
        if ((el.tagName === "A" || el.tagName === "BUTTON" || el.classList.contains("sidebar-switch-btn")) && el.hasAttribute("data-i18n")) {
            const span = el.querySelector(":scope > span:not([data-i18n])");
            if (span) {
                span.textContent = text;
                return;
            }
        }
        if (el.children.length > 0) {
            let replaced = false;
            Array.from(el.childNodes).forEach((n) => {
                if (n.nodeType === Node.TEXT_NODE && n.textContent.trim()) {
                    const lead = n.textContent.match(/^\s*/)[0];
                    const trail = n.textContent.match(/\s*$/)[0];
                    n.textContent = lead + text + trail;
                    replaced = true;
                }
            });
            if (replaced) return;
            el.appendChild(document.createTextNode(" " + text));
            return;
        }
        el.textContent = text;
    }

    function translateTextNode(node) {
        if (!node || node.nodeType !== Node.TEXT_NODE) return;
        if (node.parentElement && (node.parentElement.closest("script,style,code,pre,[data-no-i18n],input,textarea,option"))) return;
        // Never rewrite pure numbers / currency-only / empty
        const trimmed = (node._aicaOrig != null ? node._aicaOrig : node.textContent).trim();
        if (!trimmed || /^[\d₹$.,\s:%+\-–—/]+$/.test(trimmed)) return;
        if (node._aicaOrig == null) node._aicaOrig = node.textContent;
        const orig = node._aicaOrig;
        const key = orig.trim();
        if (lang === "en") {
            node.textContent = orig;
            return;
        }
        const hit = phrases[key];
        if (hit) {
            node.textContent = orig.replace(key, hit);
        }
    }

    function applyPhrases(root) {
        const scope = root || document.body;
        if (!scope) return;
        const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const p = node.parentElement;
                if (!p) return NodeFilter.FILTER_REJECT;
                if (p.closest("script,style,noscript,code,pre,svg,[data-no-i18n],[data-i18n]")) return NodeFilter.FILTER_REJECT;
                if (p.closest("input,textarea,select,option")) return NodeFilter.FILTER_REJECT;
                // Skip nodes that are only whitespace
                if (!node.textContent || !node.textContent.trim()) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            },
        });
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach(translateTextNode);

        // Placeholders without data-i18n-placeholder
        scope.querySelectorAll("input[placeholder], textarea[placeholder]").forEach((el) => {
            if (el.hasAttribute("data-i18n-placeholder")) return;
            if (el.closest("[data-no-i18n]")) return;
            if (el._aicaPhOrig == null) el._aicaPhOrig = el.getAttribute("placeholder") || "";
            const orig = el._aicaPhOrig;
            if (lang === "en") {
                el.setAttribute("placeholder", orig);
                return;
            }
            if (phrases[orig.trim()]) el.setAttribute("placeholder", phrases[orig.trim()]);
            else el.setAttribute("placeholder", orig);
        });

        // title attributes
        scope.querySelectorAll("[title]").forEach((el) => {
            if (el.hasAttribute("data-i18n-title") || el.closest("[data-no-i18n]")) return;
            if (el._aicaTitleOrig == null) el._aicaTitleOrig = el.getAttribute("title") || "";
            const orig = el._aicaTitleOrig;
            if (lang === "en") {
                el.setAttribute("title", orig);
                return;
            }
            if (phrases[orig.trim()]) el.setAttribute("title", phrases[orig.trim()]);
            else el.setAttribute("title", orig);
        });
    }

    function applyDom(root) {
        const scope = root || document;
        scope.querySelectorAll("[data-i18n]").forEach((el) => {
            const key = el.getAttribute("data-i18n");
            if (!key) return;
            if (el._aicaKeyOrig == null) el._aicaKeyOrig = el.getAttribute("data-i18n-default") || el.textContent.trim();
            const fallback = el._aicaKeyOrig || key;
            setTextPreservingIcons(el, t(key, fallback));
        });
        scope.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
            const key = el.getAttribute("data-i18n-placeholder");
            if (el._aicaPhKeyOrig == null) el._aicaPhKeyOrig = el.getAttribute("placeholder") || "";
            el.setAttribute("placeholder", t(key, el._aicaPhKeyOrig));
        });
        scope.querySelectorAll("[data-i18n-title]").forEach((el) => {
            const key = el.getAttribute("data-i18n-title");
            if (el._aicaTitleKeyOrig == null) el._aicaTitleKeyOrig = el.getAttribute("title") || "";
            el.setAttribute("title", t(key, el._aicaTitleKeyOrig));
        });
        scope.querySelectorAll("[data-i18n-aria]").forEach((el) => {
            const key = el.getAttribute("data-i18n-aria");
            el.setAttribute("aria-label", t(key, el.getAttribute("aria-label") || ""));
        });
        const titleEl = document.querySelector("title[data-i18n]");
        if (titleEl) {
            document.title = t(titleEl.getAttribute("data-i18n"), titleEl.textContent);
        }
        // Phrase pass covers unmarked UI strings across all pages
        applyPhrases(root ? root : document.body);
    }

    async function loadLang(code) {
        const c = supported.includes(code) ? code : "en";
        try {
            const res = await fetch(`/static/i18n/${c}.json?v=` + Date.now(), { cache: "no-cache" });
            if (!res.ok) throw new Error("missing");
            dict = await res.json();
            phrases = dict.phrases || {};
            lang = c;
            localStorage.setItem(STORAGE_KEY, c);
            document.documentElement.lang = c === "kn" ? "kn" : c === "hi" ? "hi" : "en";
            applyDom();
            const sel = document.getElementById("aicaLangSelect");
            if (sel && sel.value !== c) sel.value = c;
            const profileSel = document.getElementById("profileLangSelect");
            if (profileSel && profileSel.value !== c) profileSel.value = c;
            return c;
        } catch (e) {
            console.warn("i18n load failed", e);
            if (c !== "en") return loadLang("en");
            return "en";
        }
    }

    async function persistServer(code) {
        try {
            const fd = new FormData();
            fd.append("language", code);
            await fetch("/api/language", { method: "POST", body: fd, credentials: "same-origin" });
        } catch (e) { /* ignore */ }
    }

    async function setLanguage(code, syncServer) {
        const applied = await loadLang(code);
        if (syncServer !== false) await persistServer(applied);
        window.dispatchEvent(new CustomEvent("aica:lang", { detail: { lang: applied } }));
        return applied;
    }

    function init() {
        const stored = (localStorage.getItem(STORAGE_KEY) || "").toLowerCase();
        const server = (window.AICA_LANG || "en").toLowerCase();
        const initial = supported.includes(stored) ? stored : (supported.includes(server) ? server : "en");
        setLanguage(initial, false);
        const sel = document.getElementById("aicaLangSelect");
        if (sel) sel.addEventListener("change", () => setLanguage(sel.value, true));
    }

    window.AICA_I18N = { t, setLanguage, applyDom, applyPhrases, getLang: () => lang, supported };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
