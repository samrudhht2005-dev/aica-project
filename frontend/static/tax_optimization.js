/**
 * AI Optimization — actionable cards (status, checklist, details, IRA).
 * Does not regenerate recommendations; works from window.AICA_OPT_CARDS.
 */
(function () {
    const cards = Array.isArray(window.AICA_OPT_CARDS) ? window.AICA_OPT_CARDS : [];
    const byFp = {};
    cards.forEach((c) => { byFp[c.fingerprint] = c; });

    const STATUS_KEY = "aica_opt_status_v1";
    const DOCS_KEY = "aica_opt_docs_v1";
    const HISTORY_KEY = "aica_opt_history_v1";
    const STATUSES = ["NEW", "REVIEW", "IN PROGRESS", "COMPLETED"];

    function loadMap(key) {
        try {
            const raw = localStorage.getItem(key);
            const obj = raw ? JSON.parse(raw) : {};
            return obj && typeof obj === "object" ? obj : {};
        } catch (e) {
            return {};
        }
    }

    function saveMap(key, obj) {
        try { localStorage.setItem(key, JSON.stringify(obj)); } catch (e) { /* ignore */ }
    }

    function loadHistory() {
        try {
            const raw = localStorage.getItem(HISTORY_KEY);
            const arr = raw ? JSON.parse(raw) : [];
            return Array.isArray(arr) ? arr : [];
        } catch (e) {
            return [];
        }
    }

    function pushHistory(entry) {
        const list = loadHistory().filter((h) => h.fingerprint !== entry.fingerprint);
        list.unshift(entry);
        saveMap(HISTORY_KEY, list.slice(0, 12));
        renderHistory();
    }

    function formatMoney(n) {
        if (window.AICA_MONEY && window.AICA_MONEY.formatInr) return window.AICA_MONEY.formatInr(n);
        const v = Number(n) || 0;
        return "₹" + v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function setStatus(fp, status, title) {
        const map = loadMap(STATUS_KEY);
        map[fp] = status;
        saveMap(STATUS_KEY, map);
        document.querySelectorAll(`.opt-card[data-fingerprint="${CSS.escape(fp)}"] [data-opt-status]`).forEach((el) => {
            el.textContent = status;
            el.dataset.status = status;
        });
        pushHistory({
            fingerprint: fp,
            title: title || (byFp[fp] && byFp[fp].title) || "Recommendation",
            status,
            date: new Date().toISOString(),
        });
    }

    function getStatus(fp) {
        return loadMap(STATUS_KEY)[fp] || "NEW";
    }

    function getDocOverrides(fp) {
        return loadMap(DOCS_KEY)[fp] || {};
    }

    function setDocOverride(fp, label, state) {
        const all = loadMap(DOCS_KEY);
        if (!all[fp]) all[fp] = {};
        all[fp][label] = state;
        saveMap(DOCS_KEY, all);
    }

    function applyDocVisual(markEl, stateEl, state) {
        if (state === "ready") {
            markEl.textContent = "✓";
            stateEl.textContent = "READY";
            markEl.parentElement.classList.add("is-ready");
            markEl.parentElement.classList.remove("is-missing");
        } else if (state === "missing") {
            markEl.textContent = "⚠";
            stateEl.textContent = "MISSING";
            markEl.parentElement.classList.add("is-missing");
            markEl.parentElement.classList.remove("is-ready");
        } else {
            markEl.textContent = "☐";
            stateEl.textContent = "CHECK";
            markEl.parentElement.classList.remove("is-ready", "is-missing");
        }
    }

    function cycleDocState(current) {
        if (current === "ready") return "missing";
        if (current === "missing") return "unknown";
        return "ready";
    }

    function renderHistory() {
        const wrap = document.getElementById("optHistoryWrap");
        const list = document.getElementById("optHistoryList");
        if (!wrap || !list) return;
        const hist = loadHistory().slice(0, 5);
        if (!hist.length) {
            wrap.hidden = true;
            return;
        }
        wrap.hidden = false;
        list.innerHTML = hist.map((h) => {
            const d = h.date ? new Date(h.date) : null;
            const when = d && !isNaN(d) ? d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "";
            return `<li><strong>${escapeHtml(h.title)}</strong> — ${escapeHtml(h.status)}${when ? ` <span class="text-muted">(${when})</span>` : ""}</li>`;
        }).join("");
    }

    function escapeHtml(str) {
        return String(str || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    let activeCard = null;
    let modal = null;
    let portalUrlPending = "";

    function cardFromEl(el) {
        const root = el.closest(".opt-card");
        if (!root) return null;
        return byFp[root.getAttribute("data-fingerprint")] || null;
    }

    function openDetails(card) {
        activeCard = card;
        if (!modal) {
            const el = document.getElementById("optDetailModal");
            if (!el || !window.bootstrap) return;
            modal = new bootstrap.Modal(el);
        }
        document.getElementById("optDetailCategory").textContent = card.category || "Optimization";
        document.getElementById("optDetailTitle").textContent = card.title || "";
        document.getElementById("optDetailImpactLabel").textContent = card.impact_label || "Estimated amount";
        document.getElementById("optDetailBenefit").textContent = formatMoney(card.estimated_tax_impact);
        document.getElementById("optDetailBenefit").className = "opt-benefit-value " + (card.css_class || "money-neutral");
        document.getElementById("optDetailWhat").textContent = card.reason || "";
        document.getElementById("optDetailDetected").textContent = card.detected_item
            ? ("Detected: " + card.detected_item)
            : "";
        document.getElementById("optDetailWhy").textContent = card.why_aica || card.reason || "";
        document.getElementById("optDetailEligibility").textContent = card.eligibility_conditions || "";
        document.getElementById("optDetailDisclaimer").textContent = card.disclaimer || "";

        const docsUl = document.getElementById("optDetailDocs");
        const overrides = getDocOverrides(card.fingerprint);
        docsUl.innerHTML = (card.documents || []).map((d) => {
            const state = overrides[d.label] || d.status || "unknown";
            const mark = state === "ready" ? "✓" : state === "missing" ? "⚠" : "☐";
            const label = state === "ready" ? "READY" : state === "missing" ? "MISSING" : "CHECK";
            return `<li class="opt-doc-item"><span class="opt-doc-mark">${mark}</span> <span>${escapeHtml(d.label)}</span> <span class="opt-doc-state">${label}</span></li>`;
        }).join("") || "<li class='text-muted small'>No documents listed.</li>";

        const stepsOl = document.getElementById("optDetailSteps");
        stepsOl.innerHTML = (card.next_steps || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("")
            || "<li>Review the details and confirm with your tax professional.</li>";

        const portalWrap = document.getElementById("optDetailPortalWrap");
        const portalBtn = document.getElementById("optDetailPortalBtn");
        if (card.external && card.external.url) {
            portalWrap.hidden = false;
            document.getElementById("optDetailPortalPurpose").textContent =
                (card.external.label || "Official portal") + " — " + (card.external.purpose || "");
            portalUrlPending = card.external.url;
            portalBtn.onclick = () => confirmPortal(card.external.url, card.external.label);
        } else {
            portalWrap.hidden = true;
            portalUrlPending = "";
        }

        const internal = document.getElementById("optDetailInternal");
        if (card.internal && card.internal.path) {
            internal.hidden = false;
            internal.textContent = card.internal.label || "Open related page";
            internal.href = card.internal.path;
            internal.onclick = () => {
                setStatus(card.fingerprint, "IN PROGRESS", card.title);
            };
        } else {
            internal.hidden = true;
            internal.removeAttribute("href");
        }

        if (getStatus(card.fingerprint) === "NEW") {
            setStatus(card.fingerprint, "REVIEW", card.title);
        }
        modal.show();
    }

    function confirmPortal(url, label) {
        const name = label || "official portal";
        const ok = window.confirm(
            `Open the ${name} in a new tab?\n\nAICA will not submit anything for you. Have your documents ready before continuing.`
        );
        if (!ok) return;
        if (activeCard) setStatus(activeCard.fingerprint, "IN PROGRESS", activeCard.title);
        window.open(url, "_blank", "noopener,noreferrer");
    }

    function askIra(card) {
        if (!card) return;
        if (getStatus(card.fingerprint) === "NEW") {
            setStatus(card.fingerprint, "REVIEW", card.title);
        }
        const ctx = {
            title: card.title,
            rule_section: card.rule_section,
            category: card.category,
            detected_item: card.detected_item,
            reason: card.reason,
            why_aica: card.why_aica,
            eligibility_conditions: card.eligibility_conditions,
            estimated_tax_impact: card.estimated_tax_impact,
            documents: card.documents,
            next_steps: card.next_steps,
            internal: card.internal,
            external: card.external,
        };
        if (window.AICA_IRA && typeof window.AICA_IRA.askAboutOptimization === "function") {
            window.AICA_IRA.askAboutOptimization(ctx);
        } else {
            alert("IRA is not available on this page. Please refresh and try again.");
        }
    }

    function hydrateCard(root) {
        const fp = root.getAttribute("data-fingerprint");
        const card = byFp[fp];
        if (!card) return;

        const statusEl = root.querySelector("[data-opt-status]");
        if (statusEl) {
            const st = getStatus(fp);
            statusEl.textContent = st;
            statusEl.dataset.status = st;
        }

        const overrides = getDocOverrides(fp);
        root.querySelectorAll(".opt-doc-item[data-doc-label]").forEach((li) => {
            const label = li.getAttribute("data-doc-label");
            const mark = li.querySelector("[data-doc-mark]");
            const stateEl = li.querySelector("[data-doc-state]");
            if (!mark || !stateEl) return;
            const auto = mark.getAttribute("data-auto") || "unknown";
            const state = overrides[label] || auto;
            applyDocVisual(mark, stateEl, state);
        });
    }

    document.querySelectorAll(".opt-card").forEach(hydrateCard);
    renderHistory();

    document.getElementById("optFeed")?.addEventListener("click", (e) => {
        const t = e.target.closest("[data-opt-details], [data-opt-start], [data-opt-portal], [data-opt-ira], [data-doc-toggle], [data-opt-internal]");
        if (!t) return;

        if (t.matches("[data-doc-toggle]")) {
            e.preventDefault();
            const li = t.closest(".opt-doc-item");
            const root = t.closest(".opt-card");
            const fp = root && root.getAttribute("data-fingerprint");
            const label = li && li.getAttribute("data-doc-label");
            const mark = li && li.querySelector("[data-doc-mark]");
            const stateEl = li && li.querySelector("[data-doc-state]");
            if (!fp || !label || !mark || !stateEl) return;
            const overrides = getDocOverrides(fp);
            const auto = mark.getAttribute("data-auto") || "unknown";
            const current = overrides[label] || auto;
            const next = cycleDocState(current);
            setDocOverride(fp, label, next);
            applyDocVisual(mark, stateEl, next);
            return;
        }

        const card = cardFromEl(t);
        if (!card) return;

        if (t.matches("[data-opt-details]")) {
            e.preventDefault();
            openDetails(card);
            return;
        }
        if (t.matches("[data-opt-ira]")) {
            e.preventDefault();
            askIra(card);
            return;
        }
        if (t.matches("[data-opt-portal]")) {
            e.preventDefault();
            activeCard = card;
            confirmPortal(t.getAttribute("data-url"), t.getAttribute("data-label"));
            return;
        }
        if (t.matches("[data-opt-start]")) {
            e.preventDefault();
            const path = t.getAttribute("data-path");
            if (!path) return;
            setStatus(card.fingerprint, "IN PROGRESS", card.title);
            window.location.href = path;
            return;
        }
        if (t.matches("[data-opt-internal]")) {
            setStatus(card.fingerprint, "IN PROGRESS", card.title);
        }
    });

    document.getElementById("optDetailIra")?.addEventListener("click", () => {
        if (activeCard) {
            const inst = bootstrap.Modal.getInstance(document.getElementById("optDetailModal"));
            if (inst) inst.hide();
            askIra(activeCard);
        }
    });

    document.getElementById("optAskEmpty")?.addEventListener("click", () => {
        if (window.AICA_IRA && typeof window.AICA_IRA.askAboutOptimization === "function") {
            window.AICA_IRA.askAboutOptimization({
                title: "General business optimization scan",
                rule_section: "General",
                category: "Business Efficiency",
                reason: "The user has no optimization cards yet and asked IRA to analyze the business.",
                why_aica: "No rule-based opportunities were detected from current books.",
                eligibility_conditions: "",
                estimated_tax_impact: 0,
                documents: [],
                next_steps: [
                    "Add expenses, payroll, assets and GST-registered purchases in AICA.",
                    "Return to AI Optimization after recording more activity.",
                ],
                internal: { label: "Open Expenses", path: "/expenses" },
                external: null,
            });
        }
    });

    // Allow advancing status via double-click on badge (optional light control)
    document.getElementById("optFeed")?.addEventListener("dblclick", (e) => {
        const badge = e.target.closest("[data-opt-status]");
        if (!badge) return;
        const root = badge.closest(".opt-card");
        const fp = root && root.getAttribute("data-fingerprint");
        const card = fp && byFp[fp];
        if (!card) return;
        const cur = getStatus(fp);
        const idx = STATUSES.indexOf(cur);
        const next = STATUSES[(idx + 1) % STATUSES.length];
        // Do not claim COMPLETED via UI cycle beyond IN PROGRESS without user intent —
        // allow REVIEW <-> IN PROGRESS only from dblclick; COMPLETED requires explicit confirm
        if (next === "COMPLETED") {
            const ok = window.confirm(
                "Mark this recommendation as COMPLETED only if you have finished the real-world steps outside AICA. Continue?"
            );
            if (!ok) return;
        }
        setStatus(fp, next, card.title);
    });
})();
