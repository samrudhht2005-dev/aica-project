/**
 * Central INR display helpers. Display-only — never rescale database numbers.
 * Absolute rupees: 1607.80 means ₹1,607.80 (NOT lakhs).
 */
(function (global) {
    function toNumber(n) {
        if (n == null || n === "") return 0;
        if (typeof n === "number") return Number.isFinite(n) ? n : 0;
        const s = String(n).replace(/[₹,\s]/g, "");
        const v = parseFloat(s);
        return Number.isFinite(v) ? v : 0;
    }

    /** Indian grouping: 160780 → 1,60,780 */
    function indianGroup(intStr) {
        const neg = intStr.startsWith("-");
        let s = neg ? intStr.slice(1) : intStr;
        if (s.length <= 3) return (neg ? "-" : "") + s;
        const last3 = s.slice(-3);
        let rest = s.slice(0, -3);
        const parts = [];
        while (rest.length > 2) {
            parts.unshift(rest.slice(-2));
            rest = rest.slice(0, -2);
        }
        if (rest) parts.unshift(rest);
        return (neg ? "-" : "") + parts.join(",") + "," + last3;
    }

    function formatInr(amount, opts) {
        const o = opts || {};
        const n = toNumber(amount);
        const neg = n < 0;
        const abs = Math.abs(n);
        const whole = Math.floor(abs + 1e-9);
        const paise = Math.round((abs - whole) * 100);
        let body = indianGroup(String(whole)) + "." + String(paise).padStart(2, "0");
        if (neg) body = "-" + body;
        if (o.symbol === false) return body;
        return "₹" + body;
    }

    /** Compact alias for the SAME absolute value (display only). */
    function formatInrCompact(amount) {
        const n = toNumber(amount);
        const sign = n < 0 ? "-" : "";
        const abs = Math.abs(n);
        if (abs >= 1e7) return sign + "₹" + (abs / 1e7).toFixed(2) + "Cr";
        if (abs >= 1e5) return sign + "₹" + (abs / 1e5).toFixed(2) + "L";
        if (abs >= 1e3) return sign + "₹" + (abs / 1e3).toFixed(2) + "K";
        return formatInr(n);
    }

    /**
     * Semantic CSS class — meaning, not just sign.
     * owe → red, receive/save → green, else neutral
     */
    function moneyClass(semanticType) {
        const t = String(semanticType || "NEUTRAL").toUpperCase();
        if (["TAX_LIABILITY", "LIABILITY", "CASH_OUTFLOW", "EXPENSE", "LOSS"].includes(t)) {
            return "money-owe";
        }
        if (["TAX_CREDIT", "TAX_REFUND", "TAX_SAVING", "CASH_INFLOW", "PROFIT"].includes(t)) {
            return "money-gain";
        }
        return "money-neutral";
    }

    global.AICA_MONEY = {
        formatInr,
        formatInrCompact,
        moneyClass,
        toNumber,
        UNIT_NOTE: "All amounts are absolute INR. 1607.80 means ₹1,607.80 — not lakhs.",
    };
})(window);
