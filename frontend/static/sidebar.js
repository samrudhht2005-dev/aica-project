(function () {
    const nav = document.getElementById("aicaNavLinks");
    const mode = document.querySelector(".sidebar")?.getAttribute("data-sidebar-mode") || "org";
    const key = `aica_sidebar_scroll_${mode}`;

    if (nav) {
        function restore() {
            const y = parseInt(sessionStorage.getItem(key) || "0", 10);
            if (!Number.isNaN(y) && y > 0) nav.scrollTop = y;
        }
        function persist() {
            sessionStorage.setItem(key, String(nav.scrollTop || 0));
        }
        nav.addEventListener("scroll", () => {
            window.clearTimeout(nav._scrollTimer);
            nav._scrollTimer = window.setTimeout(persist, 80);
        }, { passive: true });
        nav.querySelectorAll("a").forEach((a) => a.addEventListener("click", persist));
        restore();
        window.addEventListener("pageshow", restore);
    }

    function initProfileMenu() {
        const profileBtn = document.getElementById("profileMenuBtn");
        const profileMenu = document.getElementById("profileMenu");
        if (!profileBtn || !profileMenu) return;
        if (profileBtn.dataset.menuBound === "1") return;
        profileBtn.dataset.menuBound = "1";

        function closeMenu() {
            profileMenu.setAttribute("hidden", "");
            profileMenu.classList.remove("open");
            profileBtn.setAttribute("aria-expanded", "false");
        }
        function openMenu() {
            profileMenu.removeAttribute("hidden");
            profileMenu.classList.add("open");
            profileBtn.setAttribute("aria-expanded", "true");
        }
        function toggleMenu(e) {
            e.preventDefault();
            e.stopPropagation();
            if (profileMenu.hasAttribute("hidden")) openMenu();
            else closeMenu();
        }

        profileBtn.addEventListener("click", toggleMenu);
        // Capture-phase outside click so nothing else can swallow the close
        document.addEventListener("click", (e) => {
            if (profileMenu.hasAttribute("hidden")) return;
            if (profileMenu.contains(e.target) || profileBtn.contains(e.target)) return;
            closeMenu();
        });
        profileMenu.querySelectorAll("a").forEach((a) => {
            a.addEventListener("click", () => closeMenu());
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initProfileMenu);
    } else {
        initProfileMenu();
    }
})();
