"""Quick regression checks for wake + analytics intent fixes."""
from desktop.launcher.voice_intents import detect_wake, match_intent, normalize_transcript
from desktop.launcher.voice_wake_verify import is_standalone_here_mishear, verify_wake_transcript


def main() -> int:
    failed = 0

    intent_cases = [
        ("here are open analytics", "OPEN_ANALYTICS"),
        ("open analytics", "OPEN_ANALYTICS"),
        ("show me analytics", "OPEN_ANALYTICS"),
        ("take me to analytics", "OPEN_ANALYTICS"),
        ("Hey Ira, open analytics", "OPEN_ANALYTICS"),
        ("Hey Ira, take me to sales", "OPEN_SALES"),
        ("open sales", "OPEN_SALES"),
    ]
    print("INTENT TESTS")
    for text, expected in intent_cases:
        m = match_intent(text)
        got = m.intent.name if m else None
        ok = got == expected
        if not ok:
            failed += 1
        print(f"  {'OK' if ok else 'FAIL'} {text!r} -> {got} (expected {expected})")

    wake_cases = [
        ("Hey Ira", True),
        ("Hey, Ira", True),
        ("Hey Aira", True),
        ("Hey Aaira", True),
        ("Hey Aida", True),
        ("Here.", False),
        ("here", False),
        ("higher", False),
    ]
    print("WAKE detect_wake")
    for text, expected in wake_cases:
        got = detect_wake(text)
        ok = got == expected
        if not ok:
            failed += 1
        print(f"  {'OK' if ok else 'FAIL'} detect_wake({text!r})={got}")

    print("WAKE verify (here mishear on short clips)")
    for text, expected in [("Here.", True), ("here", True), ("Hey Ira", True), ("higher", False)]:
        got = verify_wake_transcript(text, allow_here_mishear=True)
        ok = got == expected
        if not ok:
            failed += 1
        print(
            f"  {'OK' if ok else 'FAIL'} verify({text!r})={got} "
            f"standalone_here={is_standalone_here_mishear(text)}"
        )

    print(f"\n{'ALL OK' if failed == 0 else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
