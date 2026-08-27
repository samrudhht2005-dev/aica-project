/**
 * POS Sales Intelligence — tabs, charts, history, invoices.
 * Uses real /api/pos/* endpoints. Does not start the camera.
 */
(function () {
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
    const t = (key, fb) => (window.AICA_I18N && window.AICA_I18N.t ? window.AICA_I18N.t(key, fb) : (fb || key));
    const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    let charts = {};
    let currentRange = "7";
    let historyPage = 1;
    let intelligenceCache = null;

    function destroyChart(key) {
        if (charts[key]) {
            charts[key].destroy();
            delete charts[key];
        }
    }

    function themeColor(name, fallback) {
        const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return v || fallback;
    }

    function applyChartTheme() {
        if (typeof Chart === "undefined") return;
        const text = themeColor("--text", "#0f172a");
        const muted = themeColor("--text-muted", "#64748b");
        const grid = themeColor("--border", "rgba(15,35,70,0.10)");
        Chart.defaults.color = muted;
        Chart.defaults.borderColor = grid;
        Chart.defaults.plugins.legend.labels = Chart.defaults.plugins.legend.labels || {};
        Chart.defaults.plugins.legend.labels.color = text;
    }

    function lineChart(canvasId, labels, values, label, color) {
        destroyChart(canvasId);
        const el = document.getElementById(canvasId);
        if (!el || typeof Chart === "undefined") return;
        applyChartTheme();
        charts[canvasId] = new Chart(el, {
            type: "line",
            data: {
                labels,
                datasets: [{
                    label,
                    data: values,
                    borderColor: color || "#0f766e",
                    backgroundColor: (color || "#0f766e") + "22",
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { y: { beginAtZero: true } },
            },
        });
    }

    function barChart(canvasId, labels, values, label, color) {
        destroyChart(canvasId);
        const el = document.getElementById(canvasId);
        if (!el || typeof Chart === "undefined") return;
        applyChartTheme();
        charts[canvasId] = new Chart(el, {
            type: "bar",
            data: {
                labels,
                datasets: [{
                    label,
                    data: values,
                    backgroundColor: color || "#1d4ed8",
                    borderRadius: 6,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { y: { beginAtZero: true } },
                indexAxis: labels.length > 6 ? "y" : "x",
            },
        });
    }

    function doughnutChart(canvasId, labels, values) {
        destroyChart(canvasId);
        const el = document.getElementById(canvasId);
        if (!el || typeof Chart === "undefined") return;
        applyChartTheme();
        const palette = ["#0f766e", "#1d4ed8", "#b45309", "#be123c", "#7c3aed", "#0369a1", "#15803d", "#334155"];
        charts[canvasId] = new Chart(el, {
            type: "doughnut",
            data: {
                labels,
                datasets: [{ data: values, backgroundColor: palette.slice(0, labels.length) }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: "bottom" } },
            },
        });
    }

    function setActiveTab(tab) {
        const allowed = ["checkout", "qr-status", "overview", "analytics", "history", "invoices", "products"];
        if (!allowed.includes(tab)) tab = "checkout";
        $$(".pos-tab-panel").forEach((p) => {
            p.hidden = p.getAttribute("data-pos-panel") !== tab;
        });
        $$(".pos-shell-tab, .pos-nav-link").forEach((a) => {
            const isActive = a.getAttribute("data-pos-tab") === tab;
            a.classList.toggle("active", isActive);
        });
        if (location.hash !== "#" + tab) {
            history.replaceState(null, "", "#" + tab);
        }
        if (tab !== "checkout" && typeof window.ensureCameraOffForAnalytics === "function") {
            window.ensureCameraOffForAnalytics();
        }
        if (tab === "overview" || tab === "analytics" || tab === "products") {
            loadIntelligence();
        }
        if (tab === "history" || tab === "invoices") {
            loadHistory(tab === "invoices");
        }
        if (tab === "qr-status" && typeof window.loadPosQrStatus === "function") {
            window.loadPosQrStatus();
        }
        const newSaleBtn = $("#posQuickNewSale");
        if (newSaleBtn) {
            newSaleBtn.hidden = tab === "checkout";
        }
    }

    function setQrStatusFilter(status) {
        const allowed = { all: 1, active: 1, redeemed: 1, cancelled: 1 };
        const st = String(status || "all").toLowerCase();
        const filter = $("#posQrStatusFilter");
        if (!filter || !allowed[st]) return;
        filter.value = st;
        setActiveTab("qr-status");
        if (typeof window.loadPosQrStatus === "function") {
            window.loadPosQrStatus();
        }
    }

    async function loadIntelligence() {
        const from = $("#posDateFrom")?.value || "";
        const to = $("#posDateTo")?.value || "";
        const params = new URLSearchParams({ range: currentRange });
        if (currentRange === "custom" && from && to) {
            params.set("date_from", from);
            params.set("date_to", to);
        }
        const res = await fetch("/api/pos/intelligence?" + params.toString(), { credentials: "same-origin" });
        if (!res.ok) return;
        const data = await res.json();
        intelligenceCache = data;
        renderOverview(data);
        renderAnalytics(data);
        fillProductSelect(data.product_names || []);
        if (window.AICA_I18N) window.AICA_I18N.applyDom(document.getElementById("posIntelligenceRoot"));
    }

    function renderOverview(data) {
        const emptyEl = $("#posOverviewEmpty");
        const body = $("#posOverviewBody");
        if (data.empty) {
            if (emptyEl) emptyEl.hidden = false;
            if (body) body.hidden = true;
            return;
        }
        if (emptyEl) emptyEl.hidden = true;
        if (body) body.hidden = false;
        const td = data.today || {};
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        set("kpiTodaySales", inr(td.revenue));
        set("kpiTx", String(td.transactions || 0));
        set("kpiItems", String(td.items_sold || 0));
        set("kpiAvg", inr(td.avg_ticket));
        set("kpiGst", inr(td.gst));
        set("kpiBest", td.best_seller ? `${td.best_seller.name} (${td.best_seller.units})` : "—");
        set("kpiFast", data.has_velocity && td.fastest_moving
            ? `${td.fastest_moving.name} (${td.fastest_moving.units_per_day}/day)`
            : t("common.notEnoughHistory", "Not enough sales history yet."));

        const recentBody = $("#posRecentBody");
        if (recentBody) {
            recentBody.innerHTML = (data.recent || []).map((r) => `
                <tr class="pos-row-click" data-invoice-id="${r.id}">
                    <td>${r.invoice_number}</td>
                    <td class="text-truncate" style="max-width:220px">${r.products}</td>
                    <td>${inr(r.total)}</td>
                    <td>${r.time}</td>
                </tr>`).join("") || `<tr><td colspan="4">${t("common.emptySales")}</td></tr>`;
        }

        const topBody = $("#posTopBody");
        if (topBody) {
            topBody.innerHTML = (data.top_selling || []).map((p, i) => `
                <tr><td>${i + 1}</td><td>${p.name}</td><td>${p.units}</td><td>${inr(p.revenue)}</td></tr>
            `).join("") || `<tr><td colspan="4">${t("common.emptySales")}</td></tr>`;
        }

        const fastBody = $("#posFastBody");
        const fastEmpty = $("#posFastEmpty");
        if (!data.has_velocity) {
            if (fastEmpty) { fastEmpty.hidden = false; fastEmpty.textContent = t("common.notEnoughHistory"); }
            if (fastBody) fastBody.innerHTML = "";
        } else {
            if (fastEmpty) fastEmpty.hidden = true;
            if (fastBody) {
                fastBody.innerHTML = (data.fastest_moving || []).map((p, i) => `
                    <tr><td>${i + 1}</td><td>${p.name}</td><td>${p.units_per_day}</td><td>${p.units}</td></tr>
                `).join("");
            }
        }

        const lowBody = $("#posLowStockBody");
        if (lowBody) {
            lowBody.innerHTML = (data.low_stock || []).map((p) => `
                <tr>
                    <td>${p.name}</td>
                    <td>${p.stock}</td>
                    <td><span class="badge ${p.status === "critical" ? "bg-danger" : "bg-warning text-dark"}">${p.status === "critical" ? t("common.critical") : t("common.lowStock")}</span></td>
                </tr>
            `).join("") || `<tr><td colspan="3">${t("common.noData")}</td></tr>`;
        }
    }

    function renderAnalytics(data) {
        const emptyEl = $("#posAnalyticsEmpty");
        const body = $("#posAnalyticsBody");
        if (data.empty) {
            if (emptyEl) emptyEl.hidden = false;
            if (body) body.hidden = true;
            return;
        }
        if (emptyEl) emptyEl.hidden = true;
        if (body) body.hidden = false;

        lineChart("chartRevenue", (data.revenue_trend || []).map((x) => x.label), (data.revenue_trend || []).map((x) => x.value), t("pos.revenueTrend"), "#0f766e");
        lineChart("chartTx", (data.tx_trend || []).map((x) => x.label), (data.tx_trend || []).map((x) => x.value), t("pos.txTrend"), "#1d4ed8");
        barChart("chartProductQty", (data.top_selling || []).map((x) => x.name), (data.top_selling || []).map((x) => x.units), t("pos.productSales"), "#0369a1");
        barChart("chartProductRev", (data.top_revenue || []).map((x) => x.name), (data.top_revenue || []).map((x) => x.revenue), t("pos.productRevenue"), "#b45309");
        doughnutChart("chartDist", (data.distribution || []).map((x) => x.name), (data.distribution || []).map((x) => x.value));
    }

    function fillProductSelect(names) {
        const sel = $("#posProductSelect");
        if (!sel) return;
        const current = sel.value;
        sel.innerHTML = `<option value="">${t("pos.selectProduct")}</option>` +
            names.map((n) => `<option value="${n.replace(/"/g, "&quot;")}">${n}</option>`).join("");
        if (current) sel.value = current;
    }

    async function loadProductAnalytics() {
        const name = $("#posProductSelect")?.value || "";
        const box = $("#posProductDetail");
        if (!box) return;
        if (!name) {
            box.innerHTML = `<p class="text-muted mb-0">${t("pos.selectProduct")}</p>`;
            return;
        }
        const res = await fetch("/api/pos/product?name=" + encodeURIComponent(name), { credentials: "same-origin" });
        if (!res.ok) return;
        const d = await res.json();
        if (d.empty) {
            box.innerHTML = `<p class="text-muted mb-0">${d.message || t("common.notEnoughHistory")}</p>`;
            destroyChart("chartProdUnits");
            destroyChart("chartProdRev");
            return;
        }
        box.innerHTML = `
            <div class="row g-3 mb-3">
                <div class="col-md-3"><div class="pos-mini-kpi"><span data-i18n="pos.unitsSold">Units Sold</span><strong>${d.units_sold}</strong></div></div>
                <div class="col-md-3"><div class="pos-mini-kpi"><span data-i18n="pos.revenue">Revenue</span><strong>${inr(d.revenue)}</strong></div></div>
                <div class="col-md-3"><div class="pos-mini-kpi"><span data-i18n="pos.transactions">Transactions</span><strong>${d.transactions}</strong></div></div>
                <div class="col-md-3"><div class="pos-mini-kpi"><span data-i18n="pos.avgQtyPerTx">Avg. qty / transaction</span><strong>${d.avg_qty_per_tx}</strong></div></div>
            </div>
            ${d.enough_history ? "" : `<p class="text-muted small">${t("common.notEnoughHistory")}</p>`}
            <div class="row g-3 mb-3">
                <div class="col-md-6"><div class="pos-chart-wrap"><canvas id="chartProdUnits"></canvas></div></div>
                <div class="col-md-6"><div class="pos-chart-wrap"><canvas id="chartProdRev"></canvas></div></div>
            </div>
            <h6 data-i18n="pos.recentSales">Recent sales</h6>
            <div class="table-responsive"><table class="table table-sm"><thead><tr><th data-i18n="pos.invoiceNo">Invoice</th><th data-i18n="common.date">Date</th><th data-i18n="pos.qty">Qty</th><th data-i18n="common.total">Total</th></tr></thead>
            <tbody>${(d.recent || []).map((r) => `<tr class="pos-row-click" data-invoice-id="${r.id}"><td>${r.invoice_number}</td><td>${r.created_at}</td><td>${r.qty}</td><td>${inr(r.total)}</td></tr>`).join("")}</tbody></table></div>`;
        if (window.AICA_I18N) window.AICA_I18N.applyDom(box);
        lineChart("chartProdUnits", (d.units_trend || []).map((x) => x.label), (d.units_trend || []).map((x) => x.value), t("pos.unitsSold"), "#0f766e");
        lineChart("chartProdRev", (d.revenue_trend || []).map((x) => x.label), (d.revenue_trend || []).map((x) => x.value), t("pos.revenue"), "#1d4ed8");
    }

    async function fetchHistory(forInvoices) {
        const q = forInvoices ? ($("#posInvSearch")?.value || "") : ($("#posHistSearch")?.value || "");
        const date_from = forInvoices ? ($("#posInvFrom")?.value || "") : ($("#posHistFrom")?.value || "");
        const date_to = forInvoices ? ($("#posInvTo")?.value || "") : ($("#posHistTo")?.value || "");
        const params = new URLSearchParams({
            q, page: String(historyPage), page_size: "15",
        });
        if (date_from) params.set("date_from", date_from);
        if (date_to) params.set("date_to", date_to);
        const res = await fetch("/api/pos/history?" + params.toString(), { credentials: "same-origin" });
        if (!res.ok) return null;
        return res.json();
    }

    function renderPager(pager, data, forInvoices) {
        if (!pager) return;
        pager.innerHTML = `
                <button type="button" class="btn btn-sm btn-outline-secondary" ${data.page <= 1 ? "disabled" : ""} data-page="${data.page - 1}">${t("common.prev", "Previous")}</button>
                <span class="small mx-2">${t("common.page", "Page")} ${data.page} ${t("common.of", "of")} ${data.pages}</span>
                <button type="button" class="btn btn-sm btn-outline-secondary" ${data.page >= data.pages ? "disabled" : ""} data-page="${data.page + 1}">${t("common.next", "Next")}</button>`;
    }

    async function loadHistory(forInvoices) {
        const data = await fetchHistory(forInvoices);
        if (!data) return;
        if (forInvoices) {
            renderInvoiceRows(data);
        } else {
            renderHistoryRows(data);
        }
    }

    function renderHistoryRows(data) {
        const tbody = $("#posHistBody");
        if (!tbody) return;
        if (!data.items.length) {
            tbody.innerHTML = `<tr><td colspan="7">${t("common.emptySales", "No sales recorded yet.")}</td></tr>`;
        } else {
            tbody.innerHTML = data.items.map((r) => `
                <tr>
                    <td>${r.invoice_number}</td>
                    <td>${r.date}</td>
                    <td>${r.time}</td>
                    <td class="text-truncate" style="max-width:280px" title="${String(r.products || "").replace(/"/g, "&quot;")}">${r.products}</td>
                    <td>${r.payment_method || "PoS"}</td>
                    <td>${inr(r.grand_total)}</td>
                    <td>
                        <button type="button" class="btn btn-sm btn-outline-primary" data-invoice-id="${r.id}">${t("pos.viewSale", "View sale")}</button>
                    </td>
                </tr>`).join("");
        }
        renderPager($("#posHistPager"), data, false);
    }

    function renderInvoiceRows(data) {
        const tbody = $("#posInvBody");
        if (!tbody) return;
        const customer = t("pos.walkInCustomer", "Walk-in Customer");
        if (!data.items.length) {
            tbody.innerHTML = `<tr><td colspan="8">${t("common.emptySales", "No sales recorded yet.")}</td></tr>`;
        } else {
            tbody.innerHTML = data.items.map((r) => `
                <tr>
                    <td>${r.invoice_number}</td>
                    <td>${r.date}</td>
                    <td>${customer}</td>
                    <td>${inr(r.subtotal)}</td>
                    <td>${inr(r.total_tax)}</td>
                    <td>${inr(r.grand_total)}</td>
                    <td>${r.status || "Completed"}</td>
                    <td>
                        <button type="button" class="btn btn-sm btn-outline-primary" data-invoice-id="${r.id}">${t("pos.viewInvoice", "View Invoice")}</button>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-download-invoice="${r.id}">${t("pos.downloadInvoice", "Download PDF")}</button>
                    </td>
                </tr>`).join("");
        }
        renderPager($("#posInvPager"), data, true);
    }

    async function downloadInvoiceFile(transactionId) {
        if (!transactionId) return;
        try {
            const res = await fetch("/download_invoice/" + transactionId, {
                credentials: "same-origin",
                headers: { "Accept": "application/pdf, application/json" },
            });
            const contentType = (res.headers.get("Content-Type") || "").toLowerCase();
            if (!res.ok) {
                let msg = t("pos.downloadFailed", "Could not download this invoice.");
                try {
                    const err = await res.json();
                    if (err && (err.error || err.detail)) msg = err.error || err.detail;
                } catch (e) {}
                alert(msg);
                return;
            }
            if (contentType.includes("text/html")) {
                alert(t("pos.downloadFailed", "Could not download this invoice. Please sign in again."));
                return;
            }
            if (contentType.includes("application/json")) {
                const data = await res.json();
                alert(data.error || data.message || t("pos.downloadFailed", "Could not download this invoice."));
                return;
            }
            const blob = await res.blob();
            let filename = "invoice.pdf";
            const contentDisposition = res.headers.get("Content-Disposition") || "";
            const filenameMatch = contentDisposition.match(/filename\*?=(?:UTF-8''|"?)([^";]+)"?/i);
            if (filenameMatch && filenameMatch[1]) {
                filename = decodeURIComponent(filenameMatch[1].trim());
            }
            if (typeof window.AICA_downloadBlob === "function") {
                const saved = await window.AICA_downloadBlob(blob, filename);
                if (saved && saved.cancelled) return;
                if (saved && !saved.ok && saved.error) {
                    alert(saved.error);
                }
            } else {
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                window.URL.revokeObjectURL(url);
            }
        } catch (e) {
            alert(t("pos.downloadFailed", "Could not download this invoice."));
        }
    }

    async function openInvoice(id) {
        const res = await fetch("/api/pos/invoice/" + id, { credentials: "same-origin" });
        if (!res.ok) return;
        const inv = await res.json();
        const modal = $("#posInvoiceModal");
        const body = $("#posInvoiceModalBody");
        if (!body || !modal) return;
        const org = inv.organization || {};
        body.innerHTML = `
            <div class="pos-invoice-sheet" id="posInvoicePrintArea">
                <div class="d-flex justify-content-between align-items-start mb-3">
                    <div>
                        <h4 class="mb-1">${org.name || "AICA"}</h4>
                        <div class="small text-muted">${org.address || ""}</div>
                        <div class="small">GSTIN: ${org.gstin || "—"}</div>
                    </div>
                    <div class="text-end">
                        <strong>${inv.invoice_number}</strong>
                        <div class="small">${inv.created_at}</div>
                        <div class="small">${inv.payment_method} · ${inv.status}</div>
                    </div>
                </div>
                <div class="small mb-2">Customer: ${(inv.customer && inv.customer.name) || "Walk-in Customer"}</div>
                <table class="table table-sm">
                    <thead><tr><th>Product</th><th>Qty</th><th>Price</th><th>Subtotal</th><th>Tax</th><th>Total</th></tr></thead>
                    <tbody>
                        ${(inv.lines || []).map((l) => `
                            <tr>
                                <td>${l.product || ""}</td>
                                <td>${l.qty}</td>
                                <td>${inr(l.price)}</td>
                                <td>${inr(l.subtotal)}</td>
                                <td>${inr(l.gst_amt)}</td>
                                <td>${inr(l.total)}</td>
                            </tr>`).join("")}
                    </tbody>
                </table>
                <div class="text-end">
                    <div>Subtotal: ${inr(inv.subtotal)}</div>
                    <div>CGST: ${inr(inv.cgst)} · SGST: ${inr(inv.sgst)} · IGST: ${inr(inv.igst)}</div>
                    <div>Total tax: ${inr(inv.total_tax)}</div>
                    <h5 class="mt-2">Grand Total: ${inr(inv.grand_total)}</h5>
                </div>
            </div>
            <div class="d-flex gap-2 mt-3">
                <button type="button" class="btn btn-primary btn-sm" id="posPrintInvoiceBtn">${t("pos.printInvoice")}</button>
                <button type="button" class="btn btn-outline-secondary btn-sm" id="posDownloadInvoiceBtn" data-download-invoice="${inv.id}">${t("pos.downloadInvoice")}</button>
            </div>`;
        modal.hidden = false;
        $("#posPrintInvoiceBtn")?.addEventListener("click", () => {
            const area = $("#posInvoicePrintArea");
            if (!area) return;
            const w = window.open("", "_blank");
            w.document.write(`<html><head><title>${inv.invoice_number}</title>
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
                </head><body class="p-4">${area.innerHTML}</body></html>`);
            w.document.close();
            w.focus();
            w.print();
        });
        $("#posDownloadInvoiceBtn")?.addEventListener("click", () => downloadInvoiceFile(inv.id));
    }

    function bind() {
        $$(".pos-shell-tab, .pos-nav-link").forEach((el) => {
            el.addEventListener("click", (e) => {
                const tab = el.getAttribute("data-pos-tab");
                if (!tab) return;
                if (el.tagName === "A" && el.getAttribute("href")?.startsWith("/pos")) {
                    e.preventDefault();
                }
                setActiveTab(tab);
            });
        });

        $$(".pos-range-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                currentRange = btn.getAttribute("data-range") || "7";
                $$(".pos-range-btn").forEach((b) => b.classList.toggle("active", b === btn));
                const custom = $("#posCustomRange");
                if (custom) custom.hidden = currentRange !== "custom";
                loadIntelligence();
            });
        });

        $("#posApplyCustom")?.addEventListener("click", () => {
            currentRange = "custom";
            loadIntelligence();
        });

        $("#posProductSelect")?.addEventListener("change", loadProductAnalytics);

        document.addEventListener("click", (e) => {
            const dlBtn = e.target.closest("[data-download-invoice]");
            if (dlBtn) {
                e.preventDefault();
                downloadInvoiceFile(dlBtn.getAttribute("data-download-invoice"));
                return;
            }
            const invBtn = e.target.closest("[data-invoice-id]");
            if (invBtn) {
                openInvoice(invBtn.getAttribute("data-invoice-id"));
                return;
            }
            const pageBtn = e.target.closest("[data-page]");
            if (pageBtn && pageBtn.closest("#posHistPager, #posInvPager")) {
                historyPage = parseInt(pageBtn.getAttribute("data-page"), 10) || 1;
                const forInv = !!pageBtn.closest("#posInvPager");
                loadHistory(forInv);
            }
        });

        $("#posHistApply")?.addEventListener("click", () => { historyPage = 1; loadHistory(false); });
        $("#posInvApply")?.addEventListener("click", () => { historyPage = 1; loadHistory(true); });
        $("#posInvoiceModalClose")?.addEventListener("click", () => { $("#posInvoiceModal").hidden = true; });
        $("#posInvoiceModalBackdrop")?.addEventListener("click", () => { $("#posInvoiceModal").hidden = true; });

        $("#posQuickNewSale")?.addEventListener("click", () => setActiveTab("checkout"));

        window.addEventListener("aica:lang", () => {
            if (intelligenceCache) {
                renderOverview(intelligenceCache);
                renderAnalytics(intelligenceCache);
            }
            if (window.AICA_I18N) window.AICA_I18N.applyDom(document.getElementById("posIntelligenceRoot"));
        });

        const hash = (location.hash || "#checkout").replace("#", "");
        setActiveTab(hash || "checkout");
        window.addEventListener("hashchange", () => {
            const next = (location.hash || "#checkout").replace("#", "") || "checkout";
            setActiveTab(next);
        });
    }

    window.AICA_POS_INTEL = { setActiveTab, setQrStatusFilter, loadIntelligence, openInvoice, downloadInvoiceFile };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
    else bind();
})();
