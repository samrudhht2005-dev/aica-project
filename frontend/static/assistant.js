(function () {
    const fab = document.getElementById("aicaFab");
    const panel = document.getElementById("aicaChat");
    const closeBtn = document.getElementById("aicaChatClose");
    const form = document.getElementById("aicaChatForm");
    const input = document.getElementById("aicaChatInput");
    const body = document.getElementById("aicaChatBody");
    const navBox = document.getElementById("aicaChatNav");
    const ctxEl = document.getElementById("aicaChatContext");
    const micBtn = document.getElementById("aicaMicBtn");
    const micIcon = document.getElementById("aicaMicIcon");
    const voiceStatus = document.getElementById("aicaVoiceStatus");
    if (!fab || !panel || !form) return;

    const PAGE = window.AICA_PAGE || "dashboard";
    const PATH = window.AICA_PATH || location.pathname;
    const labels = {
        dashboard: "Dashboard",
        pos: "POS checkout",
        sales: "Sales",
        expenses: "Expenses",
        employees: "Payroll",
        assets: "Fixed assets",
        gst: "GST & ITC",
        "income-tax": "Income tax",
        "tax-optimization": "Tax optimisation",
        compliance: "Compliance",
        forecasting: "Forecasting",
        "what-if": "What-if",
        reports: "Reports",
        warehouse: "Inventory",
        organization: "Organisation",
        profile: "Profile",
    };
    if (ctxEl) ctxEl.textContent = labels[PAGE] || "This screen";

    const storageKey = "aica_assistant_state";
    function loadState() {
        try {
            return JSON.parse(sessionStorage.getItem(storageKey) || "{}");
        } catch (e) {
            return {};
        }
    }
    function saveState(state) {
        sessionStorage.setItem(storageKey, JSON.stringify(state));
    }

    let state = loadState();
    if (!Array.isArray(state.messages)) state.messages = [];
    if (!state.task) state.task = "";

    let voiceMode = "idle"; // idle | listening | thinking | speaking
    let recognition = null;
    let speakingUtterance = null;

    function escapeHTML(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function plainTextForSpeech(md) {
        return String(md || "")
            .replace(/```[\s\S]*?```/g, " ")
            .replace(/`[^`]*`/g, " ")
            .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
            .replace(/[#>*_~]/g, " ")
            .replace(/\s+/g, " ")
            .trim();
    }

    function tVoice(key, fallback) {
        if (window.AICA_I18N && window.AICA_I18N.t) return window.AICA_I18N.t(key, fallback);
        return fallback;
    }

    function setVoiceUI(mode) {
        voiceMode = mode;
        if (!micBtn || !voiceStatus) return;
        micBtn.classList.remove("listening", "thinking", "speaking");
        if (mode === "idle") {
            voiceStatus.hidden = true;
            voiceStatus.textContent = "";
            if (micIcon) micIcon.className = "bi bi-mic";
            micBtn.title = tVoice("assistant.micOff", "Voice");
        } else {
            voiceStatus.hidden = false;
            micBtn.classList.add(mode);
            if (mode === "listening") {
                voiceStatus.textContent = tVoice("assistant.listening", "Listening…");
                if (micIcon) micIcon.className = "bi bi-mic-fill";
            } else if (mode === "thinking") {
                voiceStatus.textContent = tVoice("assistant.thinking", "Thinking…");
                if (micIcon) micIcon.className = "bi bi-hourglass-split";
            } else if (mode === "speaking") {
                voiceStatus.textContent = tVoice("assistant.speaking", "Speaking…");
                if (micIcon) micIcon.className = "bi bi-volume-up-fill";
            }
        }
    }

    function stopSpeech() {
        try {
            window.speechSynthesis && window.speechSynthesis.cancel();
        } catch (e) { /* ignore */ }
        speakingUtterance = null;
    }

    function stopListening() {
        try {
            if (recognition) recognition.stop();
        } catch (e) { /* ignore */ }
    }

    function cancelVoice() {
        stopListening();
        stopSpeech();
        setVoiceUI("idle");
    }

    function speak(text) {
        if (!window.speechSynthesis) return;
        stopSpeech();
        const utter = new SpeechSynthesisUtterance(plainTextForSpeech(text));
        const lang = (window.AICA_I18N && window.AICA_I18N.getLang && window.AICA_I18N.getLang()) || "en";
        utter.lang = lang === "hi" ? "hi-IN" : lang === "kn" ? "kn-IN" : "en-IN";
        speakingUtterance = utter;
        setVoiceUI("speaking");
        utter.onend = () => {
            speakingUtterance = null;
            setVoiceUI("idle");
        };
        utter.onerror = () => {
            speakingUtterance = null;
            setVoiceUI("idle");
        };
        window.speechSynthesis.speak(utter);
    }

    function renderMessages() {
        body.innerHTML = state.messages.map((m) => {
            if (m.role === "user") {
                return `<div class="chat-message user-message"><div class="user-bubble">${escapeHTML(m.text)}</div></div>`;
            }
            const html = window.marked ? marked.parse(m.text || "") : escapeHTML(m.text || "");
            return `<div class="chat-message"><div class="ai-bubble">${html}</div></div>`;
        }).join("");
        body.scrollTop = body.scrollHeight;
    }

    function showNav(nav) {
        if (!nav || !nav.path) {
            navBox.hidden = true;
            navBox.innerHTML = "";
            return;
        }
        navBox.hidden = false;
        navBox.innerHTML = `
            <p>${escapeHTML(nav.reason || "Open another screen to continue.")}</p>
            <button type="button" class="aica-nav-go" data-path="${escapeHTML(nav.path)}">Open ${escapeHTML(nav.label || nav.path)}</button>
            <button type="button" class="aica-nav-skip">Stay here</button>
        `;
        navBox.querySelector(".aica-nav-go").addEventListener("click", () => {
            state.task = state.task || (state.messages.filter((m) => m.role === "user").slice(-1)[0] || {}).text || "";
            state.pendingIntro = true;
            saveState(state);
            window.location.href = nav.path;
        });
        navBox.querySelector(".aica-nav-skip").addEventListener("click", () => showNav(null));
    }

    function openPanel() {
        panel.hidden = false;
        fab.setAttribute("aria-expanded", "true");
        input.focus();
        if (state.messages.length === 0) {
            state.messages.push({
                role: "assistant",
                text: `I can help with ${labels[PAGE] || "this screen"} — fields, tax treatment, and what belongs in the books. I will not change pages unless you agree. Tap the microphone to talk.`,
            });
            saveState(state);
        }
        renderMessages();
        if (state.pendingIntro) {
            state.pendingIntro = false;
            saveState(state);
            sendMessage("Continue the same task on this screen. Tell me exactly what to complete here.", true, false);
        }
    }

    async function sendMessage(text, silent, speakReply) {
        if (!silent) {
            state.messages.push({ role: "user", text });
            saveState(state);
            renderMessages();
        }
        if (speakReply) setVoiceUI("thinking");

        const history = state.messages
            .filter((m) => m.role === "user" || m.role === "assistant")
            .slice(-8)
            .map((m) => ({ role: m.role === "user" ? "user" : "model", text: m.text }));

        const fd = new FormData();
        fd.append("question", text);
        fd.append("page", PAGE);
        fd.append("path", PATH);
        fd.append("task", state.task || "");
        fd.append("history", JSON.stringify(history.slice(0, -1)));

        try {
            const res = await fetch("/api/assistant", { method: "POST", body: fd, credentials: "same-origin" });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || "Assistant unavailable");
            const answer = data.answer || "";
            state.messages.push({ role: "assistant", text: answer });
            saveState(state);
            renderMessages();
            showNav(data.navigation);
            if (speakReply) speak(answer);
            else if (voiceMode === "thinking") setVoiceUI("idle");
        } catch (err) {
            const fail = "I could not reach the assistant just now. Please try again.";
            state.messages.push({ role: "assistant", text: fail });
            saveState(state);
            renderMessages();
            if (speakReply) speak(fail);
            else setVoiceUI("idle");
        }
    }

    function startListening() {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) {
            alert("Speech recognition is not supported in this browser. Try Chrome.");
            return;
        }
        cancelVoice();
        recognition = new SR();
        recognition.lang = ((window.AICA_I18N && window.AICA_I18N.getLang && window.AICA_I18N.getLang()) === "hi")
            ? "hi-IN"
            : ((window.AICA_I18N && window.AICA_I18N.getLang && window.AICA_I18N.getLang()) === "kn")
                ? "kn-IN"
                : "en-IN";
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        recognition.onstart = () => setVoiceUI("listening");
        recognition.onerror = () => setVoiceUI("idle");
        recognition.onend = () => {
            if (voiceMode === "listening") setVoiceUI("idle");
        };
        recognition.onresult = (event) => {
            const transcript = (event.results[0][0].transcript || "").trim();
            if (!transcript) {
                setVoiceUI("idle");
                return;
            }
            if (!state.task) state.task = transcript;
            openPanel();
            sendMessage(transcript, false, true);
        };
        try {
            recognition.start();
        } catch (e) {
            setVoiceUI("idle");
        }
    }

    fab.addEventListener("click", () => {
        if (panel.hidden) openPanel();
        else {
            panel.hidden = true;
            fab.setAttribute("aria-expanded", "false");
            cancelVoice();
        }
    });
    closeBtn.addEventListener("click", () => {
        panel.hidden = true;
        fab.setAttribute("aria-expanded", "false");
        cancelVoice();
    });
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        const q = input.value.trim();
        if (!q) return;
        input.value = "";
        if (!state.task) state.task = q;
        sendMessage(q, false, false);
    });

    if (micBtn) {
        micBtn.addEventListener("click", () => {
            if (voiceMode === "listening" || voiceMode === "speaking" || voiceMode === "thinking") {
                cancelVoice();
                return;
            }
            if (panel.hidden) openPanel();
            startListening();
        });
    }

    renderMessages();
})();
