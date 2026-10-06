"""AgentCore Platform v1.0"""

# SVC-C2-018 — caller-input screens shared by the HTTP adapter and PreProcessNode.
#
# The template owns its own guarantees. The platform input gate blocks some
# prompt-injection forms, but not all of them: a chat-template control token
# written as <<SYS>> scores no high-confidence finding and reaches the pipeline
# untouched, and a directive spliced with markup ("ig<b>nore</b> all previous
# instructions") re-assembles into a directive only after the markup is removed.
# Both are screened here, in the template, so the guarantee holds wherever the
# agent is mounted.
#
# Two rules govern every screen in this module:
#   - fail CLOSED: an unrecognised or hostile value is refused, never repaired;
#   - never echo: a refusal names the FIELD and a closed-set label, never the
#     value that tripped it and never the matched substring.

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from framework.security.credential_detector import detect_credentials_in_value
from framework.security.pii_detector import detect_pii
from framework.security.pii_masking import mask_pii

# The platform privacy filter replaces each detected span with this placeholder
# before any node runs.
MASK_PLACEHOLDER = "[MASKED]"

# ── Chat-template control tokens ─────────────────────────────────────────────
# Screened as a CLASS, not as a list of literals: any <|…|> token, any [INST]
# / [/INST] marker, any <<SYS>> / <</SYS>> marker. A service request has no
# legitimate reason to carry model-turn framing.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^|>\n]{0,64}\|>"  # <|im_start|>, <|endoftext|>, …
    r"|\[/?INST\]"  # [INST] … [/INST]
    r"|<</?SYS>>",  # <<SYS>> … <</SYS>>
    re.IGNORECASE,
)

# ── Instruction-override directives ──────────────────────────────────────────
# Deliberately narrow: an imperative verb must govern an INSTRUCTION object.
# "Please ignore the previous ticket" and "the system prompt says my password
# expired" are ordinary service-desk sentences and must pass; only a directive
# aimed at the agent's own instructions is refused.
_DIRECTIVE_RE = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass|discard)\b"
    r"(?:\s+(?:all|any|the|your|our|these|those|previous|prior|above|earlier|preceding|system)\b){0,4}"
    r"\s+\b(?:instruction|instructions|rule|rules|prompt|prompts|directive|directives"
    r"|guardrail|guardrails|restriction|restrictions|policy|policies)\b",
    re.IGNORECASE,
)

# Markup that can be spliced INTO a word to break a literal match. Removing it
# lets the screen see the re-assembled directive.
_MARKUP_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]{0,15}\s*/?>")

# Closed-set refusal labels. A caller only ever sees one of these.
CONTROL_TOKEN = "chat_template_control_token"
INSTRUCTION_OVERRIDE = "instruction_override_directive"
CREDENTIAL_SHAPE = "credential_shape"

# Caller-supplied context values render into the routing response, so they are
# locked to an inert identifier alphabet — free text there would be
# caller-controlled output injection.
INERT_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# The only context keys this agent accepts. Anything else is DROPPED before the
# graph is invoked: validators ignore undeclared keys, and ignoring is not
# stripping — an undeclared key would still travel into the first node's result
# and be scanned there.
ALLOWED_CONTEXT_KEYS = ("channel",)

# Upper bound on the assembled context payload (bytes of UTF-8).
MAX_CONTEXT_BYTES = 4096


def normalise_for_screening(text: str) -> str:
    """Return the text in the form the screens compare against.

    Unicode is NFKC-folded so full-width and compatibility spellings of the
    control-token punctuation cannot slip past a literal comparison.
    """
    return unicodedata.normalize("NFKC", text)


def strip_markup(text: str) -> str:
    """Remove simple markup tags so a directive spliced with them re-assembles."""
    return _MARKUP_RE.sub("", text)


def screen_text(text: str) -> Optional[str]:
    """Screen one caller string. Returns a closed-set label, or None if clean.

    The value is screened BOTH raw and markup-stripped: a control token must be
    caught before any stripping could remove it, and a spliced directive is only
    visible after stripping.
    """
    if not isinstance(text, str) or not text:
        return None
    raw = normalise_for_screening(text)
    for candidate in (raw, strip_markup(raw)):
        if _CONTROL_TOKEN_RE.search(candidate):
            return CONTROL_TOKEN
        if _DIRECTIVE_RE.search(candidate):
            return INSTRUCTION_OVERRIDE
    return None


def screen_structure(value: Any, path: str = "") -> Optional[Tuple[str, str]]:
    """Depth-first screen of a parsed structure, KEYS included.

    Returns ``(path, label)`` for the first finding, or None. Keys are screened
    as well as values because a hostile field NAME survives a value-only scan,
    and the whole structure is screened after parsing so escape sequences in the
    wire format cannot hide a token from it.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            key_label = screen_text(str(key))
            if key_label:
                return (f"{path}.<key #{list(value).index(key) + 1}>".lstrip("."), key_label)
            found = screen_structure(item, f"{path}.{key}" if path else str(key))
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = screen_structure(item, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(value, str):
        label = screen_text(value)
        if label:
            return (path or "value", label)
    return None


def credential_findings(value: Any) -> List[str]:
    """Return the credential-finding TYPES in a value, using the platform detector.

    Delegating means this screen blocks exactly the set the platform's own
    output gate blocks — a locally maintained pattern list would drift narrower,
    and a value the platform catches but the template misses fails the request
    deep inside the graph with no actionable reason for the caller.

    Only the finding types are returned; the matched text is never surfaced.
    """
    return [finding["type"] for finding in detect_credentials_in_value(value)]


def sanitise_field_name(name: str, position: int) -> str:
    """Return a name safe to quote back to the caller.

    Field names are caller data too: a name is echoed only when it is an inert
    identifier and trips no credential pattern of its own; otherwise the caller
    gets a positional reference.
    """
    if INERT_IDENTIFIER_RE.match(name) and not credential_findings(name):
        return name
    return f"field #{position}"


def validate_context(raw: Any) -> Tuple[Dict[str, str], Optional[str]]:
    """Validate the caller-supplied context mapping.

    Returns ``(context, error)``. ``context`` contains ONLY the declared keys,
    each value confirmed to be an inert identifier — unknown keys are DROPPED
    rather than ignored, so nothing undeclared can travel onward. ``error`` is a
    caller-safe message naming the offending field, never its value.
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "input_context must be an object"
    if len(str(raw).encode("utf-8")) > MAX_CONTEXT_BYTES:
        return {}, f"input_context exceeds {MAX_CONTEXT_BYTES} bytes"

    cleaned: Dict[str, str] = {}
    for position, (key, value) in enumerate(raw.items(), start=1):
        if key not in ALLOWED_CONTEXT_KEYS:
            continue  # dropped, not ignored
        safe_name = sanitise_field_name(str(key), position)
        if not isinstance(value, str) or not INERT_IDENTIFIER_RE.match(value):
            return {}, (
                f"input_context.{safe_name} must be a lowercase identifier " "of 1-32 characters from [a-z0-9_]"
            )
        cleaned[str(key)] = value
    return cleaned, None


def text_is_fully_masked(text: str) -> bool:
    """True when the text is nothing but privacy-filter placeholders.

    Applied to text the filter has already processed.
    """
    return not text.replace(MASK_PLACEHOLDER, " ").strip()


def privacy_filter_removes_everything(text: str) -> bool:
    """True when the platform privacy filter would remove the whole request.

    Its name pattern matches long runs of capitalised words, so an ordinary
    title-cased ticket ("The Office Aircon In The Meeting Room Is Broken") is
    replaced in full and nothing classifiable survives. Detecting that here lets
    the caller be told what happened; the same condition is detected again
    inside the pipeline, because the pipeline is also reachable without this
    adapter in front of it.

    The platform's own detector and masker are used, so this cannot disagree
    with what the pipeline will actually see.
    """
    if not text.strip():
        return False
    findings = detect_pii(text)
    if not findings:
        return False
    return text_is_fully_masked(mask_pii(text, findings))


def finite_in_range(value: Any, low: float, high: float) -> Optional[float]:
    """Parse a number and confirm it is finite and within ``[low, high]``.

    Returns None when the value is not usable. NaN and the infinities parse
    happily through ``float()`` and then compare False against every bound, so
    a plain range test on them fails OPEN — the explicit finiteness test is
    what makes this fail closed.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if not (low <= number <= high):
        return None
    return number
