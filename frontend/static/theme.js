(function () {
    const savedTheme = localStorage.getItem("theme") || "dark";
    document.documentElement.setAttribute("data-theme", savedTheme);
})();

document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("aicaThemeToggle");
    const icon = document.getElementById("aicaThemeIcon");
    if (!btn) return;

    const paint = (theme) => {
        if (icon) icon.className = theme === "dark" ? "bi bi-sun-fill" : "bi bi-moon-fill";
        btn.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    };

    paint(document.documentElement.getAttribute("data-theme") || "dark");

    btn.addEventListener("click", () => {
        const active = document.documentElement.getAttribute("data-theme") || "dark";
        const next = active === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        localStorage.setItem("theme", next);
        paint(next);
    });
    // Profile menu is handled only in sidebar.js (do not dual-bind here).
});
