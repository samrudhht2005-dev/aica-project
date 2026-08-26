/**
 * IRA — Intelligent Revenue Assistant
 * Extends the existing bottom-right AICA chatbot with wake-word + ambient UX.
 * Does not replace /api/assistant or Gemini; voice feeds the same pipeline.
 */
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
    const overlay = document.getElementById("iraOverlay");
    const statusText = document.getElementById("iraStatusText");
    if (!fab || !panel || !form) return;

    const PAGE = window.AICA_PAGE || "dashboard";
    const PATH = window.AICA_PATH || location.pathname;
    const UI_MODE = window.AICA_UI_MODE || "org";
    const AMBIENT_KEY = "ira_ambient_enabled";

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

    // Keep page label in subtitle (tagline stays in title area via i18n)
    const pageLabel = labels[PAGE] || "This screen";
    if (ctxEl && !ctxEl.getAttribute("data-i18n")) {
        ctxEl.textContent = pageLabel;
    } else if (ctxEl) {
        // Show page under tagline after i18n apply
        const sub = document.createElement("div");
        sub.className = "aica-chat-page";
        sub.id = "aicaChatPageLabel";
        sub.textContent = pageLabel;
        ctxEl.insertAdjacentElement("afterend", sub);
    }

    const storageKey = "aica_assistant_state";
    function loadState() {
        try { return JSON.parse(sessionStorage.getItem(storageKey) || "{}"); }
        catch (e) { return {}; }
    }
    function saveState(state) {
        sessionStorage.setItem(storageKey, JSON.stringify(state));
    }

    let state = loadState();
    if (!Array.isArray(state.messages)) state.messages = [];
    if (!state.task) state.task = "";

    /** @type {'idle'|'wake'|'listening'|'thinking'|'responding'|'complete'} */
    let iraMode = "idle";
    let voiceMode = "idle";
    let recognition = null;
    let ambientRecognition = null;
    let speakingUtterance = null;
    let ambientActive = localStorage.getItem(AMBIENT_KEY) === "1" && !window.AICA_DESKTOP;
    let pendingConfirm = null; // { text }
    let fadeTimer = null;
    let wakeCooldownUntil = 0;
    let listenSession = null; // { active, buffer, silenceTimer, hardTimer, startedAt }
    let ambientRestartTimer = null;
    let commandRestartTimer = null;
    let optContext = null;
    let desktopVoiceActive = false;
    const IRA_DEBUG = localStorage.getItem("ira_debug") === "1" || !!window.AICA_IRA_DEBUG;

    function iraLog() {
        if (!IRA_DEBUG) return;
        try { console.log.apply(console, ["[IRA]"].concat([].slice.call(arguments))); } catch (e) {}
    }

    function hasDesktopVoiceApi() {
        try {
            return !!(window.pywebview && window.pywebview.api && window.pywebview.api.start_voice_listen);
        } catch (e) {
            return false;
        }
    }

    function hasNativeTts() {
        try {
            return hasDesktopVoiceApi() && typeof window.pywebview.api.speak_response === "function";
        } catch (e) {
            return false;
        }
    }

    function isModernVoiceBackend() {
        const b = window.AICA_VOICE_BACKEND || "";
        return b === "aica-voice-v2" || b.indexOf("aica-voice") === 0;
    }

    function isDesktopShell() {
        return !!(window.AICA_DESKTOP || window.pywebview || (window.chrome && window.chrome.webview));
    }

    let desktopPollTimer = null;

    function stopDesktopPoll() {
        if (desktopPollTimer) {
            clearInterval(desktopPollTimer);
            desktopPollTimer = null;
        }
    }

    function handleDesktopVoiceEvent(event, payload) {
        payload = payload || {};
        iraLog("desktop_voice", event, payload);
        if (event === "wake") {
            stopDesktopPoll();
            desktopVoiceActive = false;
            ambientActive = true;
            localStorage.setItem(AMBIENT_KEY, "1");
            onWakeDetected(String(payload.text || ""));
            return;
        }
        if (!listenSession || !listenSession.active) {
            if (event === "ended" && payload.mode === "wake") {
                // Ambient wake cycle ended without match — restart if still enabled
                if (ambientActive && !document.hidden && (iraMode === "idle" || iraMode === "complete")) {
                    clearTimeout(ambientRestartTimer);
                    ambientRestartTimer = setTimeout(() => startAmbientWake(), 500);
                }
                return;
            }
            if (event === "error" && payload.message) softAck(String(payload.message));
            return;
        }
        if (event === "started") {
            if (statusText) statusText.textContent = t("assistant.listening", "Listening…");
            return;
        }
        if (event === "hypothesis" || event === "partial") {
            const text = String(payload.text || "").trim();
            if (text && statusText) statusText.textContent = text;
            if (event === "partial" && text) listenSession.buffer = text;
            return;
        }
        if (event === "final") {
            const text = String(payload.text || "").trim();
            if (text) listenSession.buffer = text;
            if (statusText && text) statusText.textContent = text;
            return;
        }
        if (event === "ended") {
            stopDesktopPoll();
            desktopVoiceActive = false;
            const text = String(
                (payload && payload.transcript) || (listenSession && listenSession.buffer) || ""
            ).trim();
            listenSession.active = false;
            clearListenTimers();
            listenSession = null;
            if (!text) {
                endListenGracefully(t("assistant.didntCatch", "I didn't catch that — try the mic again."));
                return;
            }
            // Backend intent routing (faster-whisper path)
            if (payload && payload.intent_path && payload.intent) {
                openPanel();
                state.messages.push({ role: "user", text: text });
                const speakLine = navSpeakForIntent(payload.intent) || "Done.";
                state.messages.push({ role: "assistant", text: speakLine });
                saveState(state);
                renderMessages();
                speak(speakLine, () => {
                    state.pendingIntro = true;
                    saveState(state);
                    window.location.href = payload.intent_path;
                });
                return;
            }
            handleUserCommand(text, true);
            return;
        }
        if (event === "error") {
            stopDesktopPoll();
            desktopVoiceActive = false;
            endListenGracefully(String(payload.message || "Microphone error"));
        }
    }

    /** Desktop System.Speech → JS (events arrive via poll_voice_events). */
    window.AICA_DESKTOP_VOICE = handleDesktopVoiceEvent;

    function startDesktopEventPoll() {
        stopDesktopPoll();
        desktopPollTimer = setInterval(() => {
            if (!hasDesktopVoiceApi()) return;
            Promise.resolve(window.pywebview.api.poll_voice_events())
                .then((rows) => {
                    if (!rows || !rows.length) return;
                    for (let i = 0; i < rows.length; i++) {
                        const row = rows[i] || {};
                        handleDesktopVoiceEvent(row.event || row[0], row.payload || row[1] || {});
                    }
                })
                .catch(() => { /* ignore poll blips */ });
        }, 180);
    }

    const NAV_ROUTES = [
        { re: /\b(open|go to|show|take me to)\s+(the\s+)?dashboard\b/i, path: "/", label: "Dashboard", speak: "Opening Dashboard." },
        { re: /\b(open|go to|show|take me to)\s+(the\s+)?(expenses?|expense ledger)\b/i, path: "/expenses", label: "Expenses", speak: "Sure, opening your expenses." },
        { re: /\b(open|go to|show)\s+(the\s+)?(payroll|employees?)\b/i, path: "/employees", label: "Payroll", speak: "Opening Payroll." },
        { re: /\b(open|go to|show)\s+(the\s+)?(gst|g\.?s\.?t\.?|itc)\b/i, path: "/gst", label: "GST & ITC", speak: "Opening GST and ITC." },
        { re: /\b(open|go to|show)\s+(the\s+)?(inventory|warehouse|stock)\b/i, path: "/warehouse", label: "Warehouse", speak: "Opening Inventory." },
        { re: /\b(open|go to|show)\s+(the\s+)?(pos|point of sale|checkout|scanner)\b/i, path: "/pos", label: "POS", speak: "Sure, switching you to POS." },
        { re: /\b(open|go to|show|take me to)\s+(the\s+)?(sales|sales analytics|analytics)\b/i, path: UI_MODE === "pos" ? "/pos#overview" : "/sales", label: "Sales", speak: "Sure, opening sales." },
        { re: /\b(switch to|go to|open)\s+(the\s+)?(pos|point of sale|checkout|scanner|billing)\b/i, path: "/pos", label: "POS", speak: "Sure, switching you to POS." },
        { re: /\b(open|go to|show)\s+(the\s+)?(income tax|incometax)\b/i, path: "/income-tax", label: "Income Tax", speak: "Opening Income Tax." },
        { re: /\b(open|go to|show)\s+(the\s+)?(fixed assets?|assets)\b/i, path: "/assets", label: "Fixed Assets", speak: "Opening Fixed Assets." },
        { re: /\b(open|go to|show)\s+(the\s+)?(reports?)\b/i, path: "/reports", label: "Reports", speak: "Opening Reports." },
        { re: /\b(open|go to|show)\s+(the\s+)?(compliance)\b/i, path: "/compliance", label: "Compliance", speak: "Opening Compliance." },
        { re: /\b(open|go to|show)\s+(the\s+)?(forecasting|forecast)\b/i, path: "/forecasting", label: "Forecasting", speak: "Opening Forecasting." },
        { re: /\b(open|go to|show)\s+(the\s+)?(what[- ]?if|simulator)\b/i, path: "/what-if", label: "What-If", speak: "Opening What-If Simulator." },
        { re: /\b(open|go to|show)\s+(the\s+)?(ai\s+)?(optimization|tax optimization|tax planning)\b/i, path: "/tax-optimization", label: "AI Optimization", speak: "Opening AI Optimization." },
        { re: /\b(open|go to|show)\s+(the\s+)?(organization|organisation|company|profile settings)\b/i, path: "/organization", label: "Organization", speak: "Opening Organization settings." },
        { re: /\b(open|go to|show)\s+(my\s+)?profile\b/i, path: "/profile", label: "Profile", speak: "Opening Profile." },
        { re: /\b(switch to|open)\s+(organization|organisation)\s*(interface|mode)?\b/i, path: "/select-interface", label: "Interface", speak: "Opening interface selection." },
    ];

    const MUTATE_RE = /\b(add|create|delete|remove|record|save|update|post|void|clear all|debit|credit)\b/i;
    const CONFIRM_RE = /^(yes|yeah|yep|confirm|do it|go ahead|proceed|ok|okay)\b/i;
    const CANCEL_RE = /^(no|nope|cancel|stop|never ?mind)\b/i;
    const WAKE_RE = /\b(hey|hay|hi|he)\s*[,.-]?\s*(ira|aira|era|ara)\b|\bheira\b|\bhaira\b/i;

    function t(key, fallback) {
        if (window.AICA_I18N && window.AICA_I18N.t) return window.AICA_I18N.t(key, fallback);
        return fallback;
    }

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

    function speechLang() {
        const lang = (window.AICA_I18N && window.AICA_I18N.getLang && window.AICA_I18N.getLang()) || "en";
        return lang === "hi" ? "hi-IN" : lang === "kn" ? "kn-IN" : "en-IN";
    }

    function getSR() {
        return window.SpeechRecognition || window.webkitSpeechRecognition || null;
    }

    function setFabState(mode) {
        fab.classList.remove("ira-listening", "ira-thinking", "ira-responding");
        if (mode === "listening" || mode === "wake") fab.classList.add("ira-listening");
        else if (mode === "thinking") fab.classList.add("ira-thinking");
        else if (mode === "responding") fab.classList.add("ira-responding");
    }

    function setIraMode(mode, label) {
        iraMode = mode;
        setFabState(mode);
        if (!overlay) return;

        if (mode === "idle" || mode === "complete") {
            if (mode === "complete") {
                overlay.classList.add("ira-fading");
                clearTimeout(fadeTimer);
                fadeTimer = setTimeout(() => {
                    overlay.hidden = true;
                    overlay.classList.remove("ira-active", "ira-fading", "ira-listening", "ira-thinking", "ira-responding");
                    document.body.classList.remove("ira-focus");
                    overlay.setAttribute("aria-hidden", "true");
                    setIraMode("idle");
                }, 700);
                return;
            }
            overlay.hidden = true;
            overlay.classList.remove("ira-active", "ira-fading", "ira-listening", "ira-thinking", "ira-responding");
            document.body.classList.remove("ira-focus");
            overlay.setAttribute("aria-hidden", "true");
            return;
        }

        overlay.hidden = false;
        overlay.classList.add("ira-active");
        overlay.classList.remove("ira-fading");
        clearTimeout(fadeTimer);
        fadeTimer = null;
        overlay.classList.toggle("ira-listening", mode === "listening" || mode === "wake");
        overlay.classList.toggle("ira-thinking", mode === "thinking");
        overlay.classList.toggle("ira-responding", mode === "responding");
        document.body.classList.add("ira-focus");
        overlay.setAttribute("aria-hidden", "false");
        if (statusText) {
            statusText.textContent = label || (
                mode === "wake" || mode === "listening" ? t("assistant.listening", "Listening…")
                    : mode === "thinking" ? t("assistant.thinking", "Thinking…")
                        : mode === "responding" ? t("assistant.speaking", "Speaking…")
                            : "IRA"
            );
        }
    }

    function setVoiceUI(mode) {
        voiceMode = mode;
        if (!micBtn || !voiceStatus) return;
        micBtn.classList.remove("listening", "thinking", "speaking");
        if (mode === "idle") {
            voiceStatus.hidden = true;
            voiceStatus.textContent = "";
            if (micIcon) micIcon.className = "bi bi-mic";
            micBtn.title = t("assistant.micOff", "Voice");
        } else {
            voiceStatus.hidden = false;
            micBtn.classList.add(mode === "speaking" ? "speaking" : mode);
            if (mode === "listening") {
                voiceStatus.textContent = t("assistant.listening", "Listening…");
                if (micIcon) micIcon.className = "bi bi-mic-fill";
            } else if (mode === "thinking") {
                voiceStatus.textContent = t("assistant.thinking", "Thinking…");
                if (micIcon) micIcon.className = "bi bi-hourglass-split";
            } else if (mode === "speaking") {
                voiceStatus.textContent = t("assistant.speaking", "Speaking…");
                if (micIcon) micIcon.className = "bi bi-volume-up-fill";
            }
        }
    }

    function stopSpeech() {
        try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
        speakingUtterance = null;
    }

    function clearListenTimers() {
        if (listenSession) {
            clearTimeout(listenSession.silenceTimer);
            clearTimeout(listenSession.hardTimer);
        }
        clearTimeout(commandRestartTimer);
        commandRestartTimer = null;
    }

    function stopCommandListening() {
        clearListenTimers();
        stopDesktopPoll();
        if (listenSession) listenSession.active = false;
        if (desktopVoiceActive && hasDesktopVoiceApi()) {
            desktopVoiceActive = false;
            try { window.pywebview.api.cancel_voice_listen(); } catch (e) { /* ignore */ }
        }
        try { if (recognition) recognition.onend = null; recognition.stop(); } catch (e) { /* ignore */ }
        recognition = null;
    }

    function stopAmbient() {
        clearTimeout(ambientRestartTimer);
        ambientRestartTimer = null;
        try {
            if (ambientRecognition) {
                ambientRecognition.onend = null;
                ambientRecognition.stop();
            }
        } catch (e) { /* ignore */ }
        ambientRecognition = null;
        if (hasDesktopVoiceApi()) {
            try { window.pywebview.api.stop_wake_listen(); } catch (e) { /* ignore */ }
        }
    }

    function cancelVoice() {
        stopCommandListening();
        stopSpeech();
        listenSession = null;
        pendingConfirm = null;
        setVoiceUI("idle");
        if (iraMode !== "idle") setIraMode("complete");
        setTimeout(() => { if (ambientActive) startAmbientWake(); }, 1000);
    }

    /** Soft spoken ack — does not steal listening focus / does not end the halo. */
    function softAck(text) {
        const plain = plainTextForSpeech(text);
        if (!plain) return;
        // Desktop modern voice: Piper (native) — not robotic Web Speech.
        if (hasNativeTts() && isModernVoiceBackend()
            && typeof window.pywebview.api.speak_response_async === "function") {
            try {
                Promise.resolve(window.pywebview.api.speak_response_async(plain)).catch(function () {});
            } catch (e) { /* ignore */ }
            return;
        }
        if (!window.speechSynthesis) return;
        try {
            const utter = new SpeechSynthesisUtterance(plain);
            utter.lang = speechLang();
            utter.rate = 1.08;
            utter.volume = 0.9;
            window.speechSynthesis.speak(utter);
        } catch (e) { /* ignore */ }
    }

    function navSpeakForIntent(intentName) {
        const map = {
            OPEN_DASHBOARD: "Sure, opening your dashboard.",
            OPEN_EXPENSES: "Sure, opening your expenses.",
            OPEN_SALES: "Sure, opening sales.",
            OPEN_POS: "Sure, switching you to POS.",
            OPEN_INVENTORY: "Sure, opening inventory.",
            OPEN_BILLING: "Sure, opening the billing counter.",
            OPEN_REPORTS: "Sure, opening reports.",
            OPEN_ANALYTICS: "Sure, opening sales analytics.",
            OPEN_ORGANIZATION: "Sure, opening organization settings.",
            OPEN_INTERFACE: "Sure, opening interface selection.",
        };
        return map[intentName] || null;
    }

    function speak(text, onDone) {
        const plain = plainTextForSpeech(text);
        if (!plain) {
            if (onDone) onDone();
            return;
        }
        if (hasNativeTts() && isModernVoiceBackend()) {
            stopSpeech();
            setVoiceUI("speaking");
            setIraMode("responding", t("assistant.speaking", "Speaking…"));
            Promise.resolve(window.pywebview.api.speak_response(plain))
                .then(() => {
                    setVoiceUI("idle");
                    if (onDone) onDone();
                    else setIraMode("complete");
                })
                .catch(() => {
                    setVoiceUI("idle");
                    if (onDone) onDone();
                    else setIraMode("complete");
                });
            return;
        }
        if (!window.speechSynthesis) {
            if (onDone) onDone();
            return;
        }
        stopSpeech();
        const utter = new SpeechSynthesisUtterance(plain);
        utter.lang = speechLang();
        utter.rate = 1.05;
        speakingUtterance = utter;
        setVoiceUI("speaking");
        setIraMode("responding", t("assistant.speaking", "Speaking…"));
        utter.onend = () => {
            speakingUtterance = null;
            setVoiceUI("idle");
            if (onDone) onDone();
            else setIraMode("complete");
        };
        utter.onerror = () => {
            speakingUtterance = null;
            setVoiceUI("idle");
            if (onDone) onDone();
            else setIraMode("complete");
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

    function showNav(nav, autoGo) {
        if (!nav || !nav.path) {
            navBox.hidden = true;
            navBox.innerHTML = "";
            return;
        }
        if (autoGo) {
            state.task = state.task || "";
            state.pendingIntro = true;
            saveState(state);
            window.location.href = nav.path;
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

    function matchNav(text) {
        const q = String(text || "").trim();
        for (const route of NAV_ROUTES) {
            if (route.re.test(q)) return route;
        }
        return null;
    }

    function stripWake(text) {
        return String(text || "").replace(WAKE_RE, " ").replace(/\s+/g, " ").trim();
    }

    function openPanel() {
        panel.hidden = false;
        fab.setAttribute("aria-expanded", "true");
        if (input) input.focus();
        if (state.messages.length === 0) {
            state.messages.push({
                role: "assistant",
                text: `I'm **IRA**, your Intelligent Revenue Assistant. I can help with ${pageLabel} — fields, tax treatment, and what belongs in the books.\n\nSay **Hey IRA** or tap the microphone to talk. I won't change financial data without your confirmation.`,
            });
            saveState(state);
        }
        renderMessages();
        if (state.pendingIntro) {
            state.pendingIntro = false;
            saveState(state);
            sendMessage("Continue the same task on this screen. Tell me exactly what to complete here.", true, false);
        }
        // Enable ambient wake after panel open (desktop uses System.Speech wake)
        ambientActive = true;
        localStorage.setItem(AMBIENT_KEY, "1");
        startAmbientWake();
    }

    async function sendMessage(text, silent, speakReply, autoNav) {
        if (!silent) {
            state.messages.push({ role: "user", text });
            saveState(state);
            renderMessages();
        }
        if (speakReply) {
            setVoiceUI("thinking");
            setIraMode("thinking", t("assistant.thinking", "Thinking…"));
        }

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
        if (optContext) {
            fd.append("opt_context", JSON.stringify(optContext));
        }

        try {
            const res = await fetch("/api/assistant", { method: "POST", body: fd, credentials: "same-origin" });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || "Assistant unavailable");
            const answer = data.answer || "";
            state.messages.push({ role: "assistant", text: answer });
            saveState(state);
            renderMessages();
            const shouldAuto = !!autoNav && data.navigation && data.navigation.path;
            showNav(data.navigation, shouldAuto);
            if (speakReply) {
                speak(answer, () => {
                    if (shouldAuto) {
                        state.pendingIntro = true;
                        saveState(state);
                        window.location.href = data.navigation.path;
                    } else {
                        setIraMode("complete");
                        setTimeout(() => { if (ambientActive) startAmbientWake(); }, 800);
                    }
                });
            } else if (voiceMode === "thinking") {
                setVoiceUI("idle");
                setIraMode("complete");
            }
        } catch (err) {
            const fail = "I could not reach IRA just now. Please try again.";
            state.messages.push({ role: "assistant", text: fail });
            saveState(state);
            renderMessages();
            if (speakReply) speak(fail);
            else setVoiceUI("idle");
            setIraMode("complete");
        }
    }

    function handleUserCommand(rawText, fromVoice) {
        let text = stripWake(rawText);
        if (!text) {
            if (fromVoice) {
                startCommandListening({ holdMs: 20000, keepTts: true });
                softAck(t("assistant.imListening", "I'm listening."));
            }
            return;
        }

        // Confirmation flow for mutating actions
        if (pendingConfirm) {
            if (CONFIRM_RE.test(text)) {
                const pending = pendingConfirm.text;
                pendingConfirm = null;
                openPanel();
                sendMessage(`User confirmed. Proceed with guidance only (do not invent ledger writes): ${pending}`, false, fromVoice, false);
                return;
            }
            if (CANCEL_RE.test(text)) {
                pendingConfirm = null;
                const msg = "Okay — I cancelled that.";
                state.messages.push({ role: "assistant", text: msg });
                saveState(state);
                renderMessages();
                if (fromVoice) speak(msg, () => setIraMode("complete"));
                else setIraMode("complete");
                return;
            }
        }

        if (MUTATE_RE.test(text) && !/^(open|go to|show)\b/i.test(text)) {
            pendingConfirm = { text };
            openPanel();
            const ask = `I can help prepare that, but I won't change financial records until you confirm.\n\nYou asked: “${text}”\n\nSay **yes** to continue with guidance, or **cancel**.`;
            state.messages.push({ role: "user", text });
            state.messages.push({ role: "assistant", text: ask });
            saveState(state);
            renderMessages();
            if (fromVoice) speak("I need your confirmation before changing financial data. Say yes to continue, or cancel.", () => {
                setIraMode("listening", t("assistant.listening", "Listening…"));
                startCommandListening({ holdMs: 16000, silenceMs: 2200, ignoreMs: 400 });
            });
            return;
        }

        const nav = matchNav(text);
        if (nav) {
            openPanel();
            state.messages.push({ role: "user", text });
            state.messages.push({ role: "assistant", text: nav.speak });
            saveState(state);
            renderMessages();
            if (fromVoice) {
                speak(nav.speak, () => {
                    state.pendingIntro = true;
                    saveState(state);
                    window.location.href = nav.path;
                });
            } else {
                state.pendingIntro = true;
                saveState(state);
                window.location.href = nav.path;
            }
            return;
        }

        if (!state.task) state.task = text;
        openPanel();
        sendMessage(text, false, fromVoice, fromVoice);
    }

    function endListenGracefully(msg) {
        stopCommandListening();
        listenSession = null;
        setVoiceUI("idle");
        if (msg) {
            softAck(msg);
            setIraMode("listening", msg);
            setTimeout(() => setIraMode("complete"), 900);
        } else {
            setIraMode("complete");
        }
        setTimeout(() => { if (ambientActive) startAmbientWake(); }, 1200);
    }

    function commitListenBuffer() {
        if (!listenSession || !listenSession.active) return;
        const transcript = String(listenSession.buffer || "").trim();
        listenSession.active = false;
        clearListenTimers();
        stopCommandListening();
        listenSession = null;
        if (!transcript) {
            endListenGracefully(t("assistant.didntCatch", "I didn't catch that — say Hey IRA again."));
            return;
        }
        handleUserCommand(transcript, true);
    }

    function scheduleSilenceCommit() {
        if (!listenSession || !listenSession.active) return;
        clearTimeout(listenSession.silenceTimer);
        // Wait for a natural pause before sending — don't cut mid-sentence
        listenSession.silenceTimer = setTimeout(() => commitListenBuffer(), listenSession.silenceMs);
    }

    function armHardDeadline() {
        if (!listenSession) return;
        // Desktop System.Speech bridge owns silence + hold timers
        if (listenSession.desktop) return;
        clearTimeout(listenSession.hardTimer);
        listenSession.hardTimer = setTimeout(() => {
            if (!listenSession || !listenSession.active) return;
            if (listenSession.buffer && listenSession.buffer.trim()) commitListenBuffer();
            else endListenGracefully(t("assistant.stillHere", "Still here — say Hey IRA when you're ready."));
        }, listenSession.holdMs);
    }

    function attachCommandRecognition(SR) {
        recognition = new SR();
        recognition.lang = speechLang();
        recognition.interimResults = true;
        recognition.maxAlternatives = 1;
        recognition.continuous = true;

        recognition.onstart = () => {
            setVoiceUI("listening");
            setIraMode("listening", t("assistant.listening", "Listening…"));
        };

        recognition.onerror = (ev) => {
            const err = ev && ev.error;
            // Chrome fires these often; keep the session alive
            if (err === "no-speech" || err === "aborted" || err === "network") return;
            if (err === "not-allowed" || err === "service-not-allowed") {
                ambientActive = false;
                localStorage.setItem(AMBIENT_KEY, "0");
                endListenGracefully("");
            }
        };

        recognition.onend = () => {
            // Browsers end sessions early — restart while we still want input
            if (!listenSession || !listenSession.active) return;
            if (document.hidden) return;
            clearTimeout(commandRestartTimer);
            commandRestartTimer = setTimeout(() => {
                if (!listenSession || !listenSession.active) return;
                try {
                    attachCommandRecognition(SR);
                    recognition.start();
                } catch (e) {
                    commandRestartTimer = setTimeout(() => {
                        if (!listenSession || !listenSession.active) return;
                        try {
                            attachCommandRecognition(SR);
                            recognition.start();
                        } catch (e2) { /* give up this tick */ }
                    }, 350);
                }
            }, 120);
        };

        recognition.onresult = (event) => {
            if (!listenSession || !listenSession.active) return;
            if (Date.now() < listenSession.ignoreUntil) return;

            let interim = "";
            let finalChunk = "";
            for (let i = event.resultIndex; i < event.results.length; i++) {
                const piece = (event.results[i][0].transcript || "").trim();
                if (!piece) continue;
                if (event.results[i].isFinal) finalChunk += (finalChunk ? " " : "") + piece;
                else interim += (interim ? " " : "") + piece;
            }

            // Ignore echo of our own soft ack
            const echo = /^(i'?m listening\.?|listening\.?)$/i;
            if (echo.test(finalChunk) || echo.test(interim)) return;

            if (finalChunk) {
                listenSession.buffer = (listenSession.buffer ? listenSession.buffer + " " : "") + finalChunk;
                listenSession.buffer = listenSession.buffer.replace(/\s+/g, " ").trim();
                if (statusText) statusText.textContent = listenSession.buffer;
                scheduleSilenceCommit();
            } else if (interim) {
                if (statusText) statusText.textContent = (listenSession.buffer ? listenSession.buffer + " " : "") + interim;
                // Keep hard deadline extended while user is actively speaking
                armHardDeadline();
                clearTimeout(listenSession.silenceTimer);
            }
        };
    }

    function startDesktopCommandListening(opts) {
        const holdMs = (opts && opts.holdMs) || 25000;
        const silenceMs = (opts && opts.silenceMs) || 1400;
        const keepTts = !!(opts && opts.keepTts);
        stopAmbient();
        stopCommandListening();
        // Stop leftover TTS unless caller is about to play a wake soft-ack (keepTts).
        if (!keepTts) {
            stopSpeech();
            if (hasDesktopVoiceApi()) {
                try {
                    if (typeof window.pywebview.api.cancel_speak === "function") {
                        window.pywebview.api.cancel_speak();
                    }
                } catch (e) { /* ignore */ }
            }
        }

        listenSession = {
            active: true,
            buffer: "",
            silenceTimer: null,
            hardTimer: null,
            startedAt: Date.now(),
            holdMs,
            silenceMs,
            ignoreUntil: 0,
            desktop: true,
        };
        desktopVoiceActive = true;
        setVoiceUI("listening");
        setIraMode("listening", t("assistant.listening", "Listening…"));
        startDesktopEventPoll();
        iraLog("desktop_listen_start", { holdMs, silenceMs, keepTts: keepTts });

        const api = window.pywebview.api;
        Promise.resolve(api.start_voice_listen(silenceMs, holdMs))
            .then((res) => {
                iraLog("desktop_listen_result", res);
                if (res && res.ok === false) {
                    stopDesktopPoll();
                    desktopVoiceActive = false;
                    endListenGracefully(
                        (res && res.error) || t("assistant.micError", "Could not start the microphone.")
                    );
                }
            })
            .catch((err) => {
                stopDesktopPoll();
                desktopVoiceActive = false;
                iraLog("desktop_listen_err", String(err));
                endListenGracefully(t("assistant.micError", "Could not start the microphone."));
            });
    }

    function startCommandListening(opts) {
        // Desktop shell: ALWAYS use Windows System.Speech — never Web Speech fallback.
        if (hasDesktopVoiceApi()) {
            startDesktopCommandListening(opts);
            return;
        }
        if (isDesktopShell()) {
            // pywebview not ready yet — wait briefly, then fail clearly (do not use Web Speech)
            setVoiceUI("listening");
            setIraMode("listening", t("assistant.listening", "Listening…"));
            let tries = 0;
            const waitApi = setInterval(() => {
                tries += 1;
                if (hasDesktopVoiceApi()) {
                    clearInterval(waitApi);
                    startDesktopCommandListening(opts);
                    return;
                }
                if (tries >= 25) {
                    clearInterval(waitApi);
                    endListenGracefully(
                        t("assistant.micError", "Desktop voice is not ready. Restart AICA and try again.")
                    );
                }
            }, 200);
            return;
        }

        const SR = getSR();
        if (!SR) {
            alert(t("assistant.speechUnsupported", "Speech recognition is not supported in this browser. Try Chrome."));
            setIraMode("complete");
            return;
        }
        stopAmbient();
        stopCommandListening();

        const holdMs = (opts && opts.holdMs) || 18000;
        const silenceMs = (opts && opts.silenceMs) || 2400;
        const ignoreMs = (opts && opts.ignoreMs) || 0;

        listenSession = {
            active: true,
            buffer: "",
            silenceTimer: null,
            hardTimer: null,
            startedAt: Date.now(),
            holdMs,
            silenceMs,
            ignoreUntil: Date.now() + ignoreMs,
        };

        setVoiceUI("listening");
        setIraMode("listening", t("assistant.listening", "Listening…"));
        armHardDeadline();

        try {
            attachCommandRecognition(SR);
            recognition.start();
        } catch (e) {
            commandRestartTimer = setTimeout(() => {
                if (!listenSession || !listenSession.active) return;
                try {
                    attachCommandRecognition(SR);
                    recognition.start();
                } catch (e2) {
                    endListenGracefully("");
                }
            }, 200);
        }
    }

    function onWakeDetected(remainder) {
        if (Date.now() < wakeCooldownUntil) return;
        wakeCooldownUntil = Date.now() + 2200;

        stopAmbient();
        clearTimeout(fadeTimer);
        stopSpeech();

        // Immediate visual wake — don't wait on TTS
        setIraMode("wake", t("assistant.imListening", "I'm listening."));
        setTimeout(() => {
            if (iraMode === "wake") setIraMode("listening", t("assistant.listening", "Listening…"));
        }, 280);

        const leftover = stripWake(remainder || "");

        if (leftover && leftover.length > 3) {
            softAck(t("assistant.imListening", "I'm listening."));
            // Command came with the wake phrase — small beat then handle
            setTimeout(() => handleUserCommand(leftover, true), 200);
            return;
        }

        // Start listen first, then Piper soft-ack so it is not cancelled by listen start.
        startCommandListening({ holdMs: 20000, silenceMs: 1400, ignoreMs: 400, keepTts: true });
        softAck(t("assistant.imListening", "I'm listening."));
    }

    function startDesktopWakeListen() {
        if (!hasDesktopVoiceApi()) return;
        if (listenSession && listenSession.active) return;
        if (iraMode !== "idle" && iraMode !== "complete") return;
        if (document.hidden) return;
        ambientActive = true;
        localStorage.setItem(AMBIENT_KEY, "1");
        startDesktopEventPoll();
        Promise.resolve(window.pywebview.api.start_wake_listen())
            .then((res) => {
                iraLog("desktop_wake_start", res);
                if (res && res.ok === false) {
                    clearTimeout(ambientRestartTimer);
                    ambientRestartTimer = setTimeout(() => startAmbientWake(), 1500);
                }
            })
            .catch(() => {
                clearTimeout(ambientRestartTimer);
                ambientRestartTimer = setTimeout(() => startAmbientWake(), 1500);
            });
    }

    function startAmbientWake() {
        // Desktop: Windows System.Speech wake listener (same backend as mic button)
        if (hasDesktopVoiceApi()) {
            startDesktopWakeListen();
            return;
        }
        if (isDesktopShell()) {
            // Wait briefly for pywebview API
            let tries = 0;
            const waitApi = setInterval(() => {
                tries += 1;
                if (hasDesktopVoiceApi()) {
                    clearInterval(waitApi);
                    startDesktopWakeListen();
                    return;
                }
                if (tries >= 20) clearInterval(waitApi);
            }, 250);
            return;
        }

        const SR = getSR();
        iraLog("startAmbientWake", {
            hasSR: !!SR,
            ambientActive,
            mode: iraMode,
            desktop: !!window.AICA_DESKTOP,
            mic: window.AICA_MIC_UNLOCKED,
        });
        if (!SR || !ambientActive) return;
        if (listenSession && listenSession.active) return;
        if (iraMode !== "idle" && iraMode !== "complete") return;
        if (document.hidden) return;

        stopAmbient();
        try {
            ambientRecognition = new SR();
            ambientRecognition.lang = "en-IN";
            ambientRecognition.continuous = true;
            ambientRecognition.interimResults = true;
            ambientRecognition.maxAlternatives = 3;

            ambientRecognition.onresult = (event) => {
                if (Date.now() < wakeCooldownUntil) return;
                if (listenSession && listenSession.active) return;
                let chunk = "";
                for (let i = event.resultIndex; i < event.results.length; i++) {
                    chunk += event.results[i][0].transcript || "";
                }
                iraLog("transcript", chunk);
                if (WAKE_RE.test(chunk)) {
                    iraLog("wake_matched", chunk);
                    onWakeDetected(chunk);
                }
            };
            ambientRecognition.onerror = (ev) => {
                iraLog("ambient_error", ev && ev.error);
                if (ev && (ev.error === "not-allowed" || ev.error === "service-not-allowed")) {
                    ambientActive = false;
                    localStorage.setItem(AMBIENT_KEY, "0");
                }
            };
            ambientRecognition.onend = () => {
                if (!ambientActive || document.hidden) return;
                if (listenSession && listenSession.active) return;
                if (iraMode !== "idle" && iraMode !== "complete") return;
                clearTimeout(ambientRestartTimer);
                ambientRestartTimer = setTimeout(() => startAmbientWake(), 280);
            };
            ambientRecognition.start();
            iraLog("listener_active");
        } catch (e) {
            iraLog("ambient_start_failed", String(e));
            clearTimeout(ambientRestartTimer);
            ambientRestartTimer = setTimeout(() => {
                if (ambientActive && (iraMode === "idle" || iraMode === "complete")) startAmbientWake();
            }, 600);
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
        handleUserCommand(q, false);
    });

    if (micBtn) {
        micBtn.addEventListener("click", () => {
            if (voiceMode === "listening" || voiceMode === "speaking" || voiceMode === "thinking" ||
                iraMode === "listening" || iraMode === "thinking" || iraMode === "responding") {
                cancelVoice();
                return;
            }
            // Mic click uses command mode; stop ambient wake first on all platforms
            ambientActive = true;
            localStorage.setItem(AMBIENT_KEY, "1");
            if (panel.hidden) openPanel();
            setIraMode("listening", t("assistant.listening", "Listening…"));
            if (hasDesktopVoiceApi() && typeof window.pywebview.api.warm_up_voice === "function") {
                Promise.resolve(window.pywebview.api.warm_up_voice()).catch(function () {});
            }
            startCommandListening({ holdMs: 20000, silenceMs: window.AICA_DESKTOP ? 1400 : 2600 });
        });
    }

    // Dismiss ambient UI with Escape
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && iraMode !== "idle") cancelVoice();
    });

    document.addEventListener("visibilitychange", () => {
        if (document.hidden) stopAmbient();
        else if (ambientActive && iraMode === "idle") startAmbientWake();
    });

    renderMessages();

    // Enable ambient wake (desktop = System.Speech; web = Web Speech)
    ambientActive = true;
    localStorage.setItem(AMBIENT_KEY, "1");
    setTimeout(() => startAmbientWake(), isDesktopShell() ? 800 : 600);

    window.AICA_IRA = {
        wake: () => onWakeDetected(""),
        startAmbient: () => {
            ambientActive = true;
            localStorage.setItem(AMBIENT_KEY, "1");
            startAmbientWake();
        },
        stopAmbient: () => { ambientActive = false; localStorage.setItem(AMBIENT_KEY, "0"); stopAmbient(); },
        diagnostics: () => ({
            speechRecognition: !!getSR(),
            ambientActive,
            iraMode,
            desktop: !!window.AICA_DESKTOP,
            micUnlocked: !!window.AICA_MIC_UNLOCKED,
            voiceBackend: window.AICA_VOICE_BACKEND || null,
            desktopMic: window.AICA_DESKTOP_MIC || null,
            hasDesktopApi: hasDesktopVoiceApi(),
            desktopVoiceActive,
        }),
        askAboutOptimization: (ctx) => {
            optContext = ctx && typeof ctx === "object" ? ctx : null;
            const title = (optContext && optContext.title) || "this recommendation";
            state.task = `Help the user with AI Optimization recommendation: ${title}. Explain eligibility, documents, and next steps. Offer to open the related AICA page if they confirm.`;
            saveState(state);
            openPanel();
            const intro =
                `You're viewing **${title}**. I already have the recommendation context from AICA — ask me why it appeared, what documents you need, or what to do next.`;
            state.messages.push({ role: "assistant", text: intro });
            saveState(state);
            renderMessages();
            if (input) {
                input.focus();
                input.placeholder = "e.g. What should I do next?";
            }
        },
        clearOptimizationContext: () => { optContext = null; },
    };
})();
