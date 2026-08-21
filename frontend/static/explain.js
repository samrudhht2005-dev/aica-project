document.addEventListener("DOMContentLoaded", function () {
    if (typeof bootstrap === "undefined") return;
    document.querySelectorAll('[data-bs-toggle="popover"]').forEach(function (el) {
        new bootstrap.Popover(el, {
            trigger: "click",
            html: true,
            container: "body",
            sanitize: false
        });
    });
    document.addEventListener("click", function (e) {
        if (e.target.closest(".ca-tip") || e.target.closest(".popover")) return;
        document.querySelectorAll(".ca-tip").forEach(function (btn) {
            const pop = bootstrap.Popover.getInstance(btn);
            if (pop) pop.hide();
        });
    });
});
