"""Deterministic redaction for Odoo Knowledge text, before any model reads it.

The Knowledge base is a working notebook for technicians, and some articles
carry passwords, PINs, API keys and card-like numbers. An agent reading the KB
must never receive those values, so they are stripped here, in code, rather
than by asking the model to look away. The card, CVV, expiry, SSN and bank rules
deliberately mirror the call pipeline's redact.py.

Usernames and IP addresses are left intact: they identify systems rather than
unlock them, and the setup guides are useless without them.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# A secret's value runs to the end of its line or table cell. Over-redacting the
# rest of a line is the safe failure; stopping at the first space would leak the
# second half of a passphrase like "Summer 2024!".
_LINE_VALUE = r"(?P<value>[^\n|]{1,120})"

# Every label rule requires a separator after the label, so prose such as
# "reset the password in Settings" survives untouched.
_LABELLED: list[tuple[str, re.Pattern[str]]] = [
    # A label alone on its line with the value on the next one, no separator.
    # This must run first: the separator rules can't see a bare newline.
    ("password-next-line", re.compile(
        r"(?P<keep>^[ \t•]*\b(?:pass\s?word|passwd|pwd|pass\s?code|pass\s?phrase"
        r"|pin(?:\s+(?:code|number))?|api[\s_-]?key|secret(?:[\s_-]?key)?|token"
        r"|wi-?fi\s+key|network\s+key)\b[ \t]*:?[ \t]*\n[ \t]*)"
        + _LINE_VALUE, re.IGNORECASE | re.MULTILINE)),
    ("password", re.compile(
        r"(?P<keep>\b(?:pass\s?word|passwd|pwd|pass\s?code|pass\s?phrase"
        r"|wi-?fi\s+key|network\s+key|wpa2?\s+key)\b\s*(?:[:=|]|-|\bis\b)\s*)"
        + _LINE_VALUE, re.IGNORECASE)),
    ("secret", re.compile(
        r"(?P<keep>\b(?:api[\s_-]?key|secret(?:[\s_-]?key)?"
        r"|(?:access|auth|refresh|bearer)[\s_-]?token|token|license\s+key"
        r"|product\s+key|activation\s+(?:code|key))\b\s*(?:[:=|]|-)\s*)"
        + _LINE_VALUE, re.IGNORECASE)),
    ("pin", re.compile(
        r"(?P<keep>\bpin(?:\s+(?:code|number))?\b\s*(?:[:=|#]|-|\bis\b)?\s*)"
        r"(?P<value>\d{3,12})\b", re.IGNORECASE)),
    ("cvv", re.compile(
        r"(?P<keep>(?:cvv|cvc|security code|card code)\D{0,15})"
        r"(?P<value>(?:\d\s?){3,4})", re.IGNORECASE)),
    ("expiry", re.compile(
        r"(?P<keep>(?:exp(?:iry|iration)?|good thru|valid thru)\D{0,10})"
        r"(?P<value>\d{1,2}\s*[/-]\s*\d{2,4})", re.IGNORECASE)),
    ("bank", re.compile(
        r"(?P<keep>(?:routing|account)\s+number\D{0,10})"
        r"(?P<value>(?:\d\s?){6,})", re.IGNORECASE)),
]

# ---- Line withholding -------------------------------------------------------
# Value-level redaction cannot be trusted on free-form technician notes: real KB
# articles hold credentials as "the manager password would be X", as "Password
# for Admin X", and as a "Pin Pad Passwords" heading followed by a bare list. So
# any line that so much as mentions a credential is withheld whole, along with
# the block beneath a credential heading. Over-withholding a harmless line like
# "reset the password in Settings" is the accepted cost; the Odoo link is always
# there for the full text.

WITHHELD = "[withheld: may contain credentials — open in Odoo]"

_CRED_WORDS = re.compile(
    r"\b(?:pass\s?words?|passwd|pwd|pass\s?codes?|pass\s?phrases?"
    r"|pins?(?!\s*-?\s*pads?)(?:\s+(?:code|number))?"   # a "pin pad" is hardware, not a PIN
    r"|api[\s_-]?keys?|secrets?|tokens?|credentials?"
    r"|log\s?-?in\s+(?:info|details|credentials)|wi-?fi\s+keys?|network\s+keys?)\b",
    re.IGNORECASE,
)
_HEADING_MAX = 60      # a credential line this short, unpunctuated, heads a block
_BLOCK_MAX = 40        # hard stop for a heading's block
_SENTENCE_END = re.compile(r"[.?!]\s*$")


def _is_prose(line: str) -> bool:
    return len(line) > _HEADING_MAX and bool(_SENTENCE_END.search(line))


def withhold_credential_lines(text: str | None) -> tuple[str, int]:
    """Return (text with credential lines withheld, number of lines withheld)."""
    if not text:
        return "", 0
    lines = text.split("\n")
    out: list[str] = []
    withheld = 0
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not _CRED_WORDS.search(line):
            out.append(lines[i])
            i += 1
            continue
        withheld += 1
        j = i + 1
        if len(line) <= _HEADING_MAX and not _SENTENCE_END.search(line):
            # Heading or unfinished sentence ("...with a password" / "of X"):
            # take the block beneath until prose resumes or a blank line ends it.
            taken = 0
            while j < len(lines) and taken < _BLOCK_MAX:
                nxt = lines[j].strip()
                if not nxt:
                    if taken:
                        break
                    j += 1
                    continue
                if _is_prose(nxt):
                    break
                withheld += 1
                taken += 1
                j += 1
        if not out or out[-1] != WITHHELD:
            out.append(WITHHELD)
        i = j
    return "\n".join(out), withheld


_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def redact(text: str | None) -> tuple[str, int]:
    """Return (redacted text, number of values removed)."""
    if not text:
        return "", 0
    count = 0

    def labelled(m: re.Match[str]) -> str:
        nonlocal count
        value = m.group("value")
        # Skip empty values and ones an earlier rule already replaced, so a
        # value is never counted twice.
        if not value.strip() or value.lstrip().startswith("["):
            return m.group(0)
        count += 1
        # Keep the value's trailing whitespace so table columns stay aligned.
        return m.group("keep") + REDACTED + value[len(value.rstrip()):]

    # Labelled rules first, so "PIN 1234" is caught before the generic digit
    # pass decides 1234 is too short to care about.
    for _kind, pattern in _LABELLED:
        text = pattern.sub(labelled, text)

    def ssn(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return REDACTED

    text = _SSN.sub(ssn, text)

    def card(m: re.Match[str]) -> str:
        nonlocal count
        raw = m.group(0)
        if _luhn_ok(raw):
            count += 1
            return "[CARD REDACTED]"
        if len(re.sub(r"\D", "", raw)) >= 13:
            count += 1
            return "[NUMBER REDACTED]"
        return raw

    text = _CARD.sub(card, text)
    return text, count
