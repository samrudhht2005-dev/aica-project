"""
Focused checks for IRA desktop wake → command handoff reliability.

Does not require a microphone or WebView. Asserts the shipped frontend
contracts that previously caused ~1/5 response rates.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSISTANT = ROOT / "frontend" / "static" / "assistant.js"
CHROME = ROOT / "frontend" / "templates" / "partials" / "chrome.html"
WEIGH = ROOT / "frontend" / "templates" / "weigh.html"
POS = ROOT / "frontend" / "templates" / "pos.html"
SIDEBAR_POS = ROOT / "frontend" / "templates" / "partials" / "sidebar_pos.html"
SIDEBAR_WEIGH = ROOT / "frontend" / "templates" / "partials" / "sidebar_weigh.html"
SIDEBAR_ORG = ROOT / "frontend" / "templates" / "partials" / "sidebar_org.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_wake_does_not_stop_desktop_poll():
    js = _read(ASSISTANT)
    # Locate wake branch and ensure it does not call stopDesktopPoll.
    idx = js.find('if (event === "wake")')
    assert idx >= 0, "wake event handler missing"
    wake_block = js[idx : idx + 350]
    assert "stopDesktopPoll()" not in wake_block, wake_block
    assert "Keep polling" in wake_block or "onWakeDetected" in wake_block


def test_wake_ended_does_not_close_active_command():
    js = _read(ASSISTANT)
    assert 'if (event === "ended" && payload.mode === "wake")' in js
    # Wake ended must return before the generic command-ended closer.
    wake_ended = js.find('if (event === "ended" && payload.mode === "wake")')
    generic_ended = js.find('if (event === "ended")', wake_ended + 1)
    assert wake_ended >= 0 and generic_ended > wake_ended
    wake_fn = js[wake_ended:generic_ended]
    assert "return;" in wake_fn
    assert "listenSession.active" in wake_fn or "listenSession" in wake_fn


def test_processing_and_listen_then_poll_order():
    js = _read(ASSISTANT)
    assert 'if (event === "processing")' in js
    assert "start_voice_listen(silenceMs, holdMs, uiMode)" in js
    # Poll starts after successful listen start (not before).
    listen_call = js.find("api.start_voice_listen(silenceMs, holdMs, uiMode)")
    assert listen_call >= 0
    after = js[listen_call : listen_call + 600]
    assert "startDesktopEventPoll()" in after
    # keepPoll on command handoff
    assert "stopCommandListening({ keepPoll: true })" in js
    assert "opts.keepPoll" in js or "opts && opts.keepPoll" in js


def test_async_nav_tts_helpers_present():
    js = _read(ASSISTANT)
    assert "function completeNavigationCommand" in js
    assert "speak_response_async" in js
    assert "function navigateToPath" in js
    assert "__AICA_IRA_INIT__" in js


def test_assistant_loaded_once_per_workspace():
    chrome = _read(CHROME)
    weigh = _read(WEIGH)
    pos = _read(POS)
    assert "assistant.js" in chrome
    assert "assistant.js" not in weigh, "weigh.html must not double-load assistant.js"
    assert "assistant.js" not in pos
    assert 'include "partials/chrome.html"' in _read(SIDEBAR_POS)
    assert 'include "partials/chrome.html"' in _read(SIDEBAR_WEIGH)
    assert 'include "partials/chrome.html"' in _read(SIDEBAR_ORG)


def test_simulated_event_lifecycle_order():
    """
    Pure-Python simulation of the event ordering contract the UI expects.
    """
    poll_active = True
    listen_active = False
    closed_by_wake_ended = False
    log = []

    def handle(event, payload):
        nonlocal poll_active, listen_active, closed_by_wake_ended
        if event == "wake":
            # Must NOT stop poll
            log.append("wake")
            listen_active = True  # command session starts
            return
        if event == "ended" and payload.get("mode") == "wake":
            if not listen_active:
                log.append("restart_ambient")
            else:
                log.append("ignore_wake_ended")
            return
        if not listen_active:
            return
        if event == "processing":
            log.append("processing")
            return
        if event == "ended" and payload.get("mode") == "command":
            listen_active = False
            poll_active = False
            log.append("command_ended")
            return
        if event == "ended":
            # BUG pattern: treating wake ended as command end
            closed_by_wake_ended = True
            listen_active = False
            log.append("BUG_closed")

    handle("wake", {})
    handle("ended", {"mode": "wake"})
    handle("started", {"mode": "command"})
    handle("processing", {})
    handle("ended", {"mode": "command", "text": "open expenses"})

    assert poll_active is False  # stopped only after command end
    assert closed_by_wake_ended is False
    assert log == ["wake", "ignore_wake_ended", "processing", "command_ended"]


if __name__ == "__main__":
    test_wake_does_not_stop_desktop_poll()
    test_wake_ended_does_not_close_active_command()
    test_processing_and_listen_then_poll_order()
    test_async_nav_tts_helpers_present()
    test_assistant_loaded_once_per_workspace()
    test_simulated_event_lifecycle_order()
    print("PASS: IRA reliability handoff contracts")
