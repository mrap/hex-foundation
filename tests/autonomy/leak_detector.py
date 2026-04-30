"""
Mechanism leak detector for the autonomy regression suite.

Scans hex responses for exposed internal vocabulary (mechanism names,
implementation details) that should never appear in user-facing output.
"""

import re

LEAK_WORDS = [
    "boi",
    "dispatch",
    "spec",
    "spec file",
    "spec.yaml",
    ".yaml spec",
    "hex-events",
    "hex_events",
    "policy",
    "policy.yaml",
    "agent-fleet",
    "agent fleet",
    "wake agent",
    "hex-agent",
    "standing order",
    "standing-order",
    "skill",
    "/hex-",
    "harness",
    "charter",
    "queue",
    "worker",
    "worktree",
    "CronCreate",
    "ScheduleWakeup",
    "blocked_type",
    "blocked_ref",
    "blocked_since",
]


def _strip_code_blocks(text: str) -> str:
    """Remove fenced code blocks so leak words inside them are ignored."""
    # Remove both ``` and ~~~ style fences, including language hints
    return re.sub(r"```[\s\S]*?```", "", text, flags=re.DOTALL)


def check_leaks(response: str, extra_words: list = None) -> list[str]:
    """
    Scan a hex response for leaked mechanism vocabulary.

    Args:
        response: The full text of hex's response to a user prompt.
        extra_words: Additional words to check beyond the default LEAK_WORDS.

    Returns:
        Sorted list of unique leaked words found (lowercased).
        Empty list means PASS.
    """
    vocab = list(LEAK_WORDS)
    if extra_words:
        vocab = vocab + list(extra_words)

    # Strip code blocks — leaks inside ``` are acceptable implementation output
    clean = _strip_code_blocks(response)

    found = set()
    lower_clean = clean.lower()
    for word in vocab:
        if word.lower() in lower_clean:
            found.add(word.lower())

    # Return in stable order: sorted, matching the input vocab order where possible
    vocab_lower = [w.lower() for w in vocab]
    ordered = [w for w in vocab_lower if w in found]
    # Deduplicate while preserving first-seen order
    seen = set()
    result = []
    for w in ordered:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result


def check_leaks_report(response: str, extra_words: list = None) -> dict:
    """
    Extended version of check_leaks that returns a full report dict.

    Returns:
        {
          "passed": bool,
          "leaked": list[str],
          "message": str,
        }
    """
    leaked = check_leaks(response, extra_words)
    passed = len(leaked) == 0
    if passed:
        message = "PASS — no mechanism vocabulary detected"
    else:
        message = f"FAIL — leaked words: {leaked}"
    return {"passed": passed, "leaked": leaked, "message": message}


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        print("Usage: leak_detector.py <response text>")
        print("       echo 'response' | python3 leak_detector.py")
        text = sys.stdin.read()

    report = check_leaks_report(text)
    print(report["message"])
    sys.exit(0 if report["passed"] else 1)
