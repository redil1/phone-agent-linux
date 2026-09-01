"""Delivery details that separate a person on the phone from a reading machine.

Everything here operates on the text about to be spoken, after the model has
produced it and before synthesis. It is deliberately deterministic: the model
is asked to be natural, but it cannot be trusted to be consistent about it, and
these are the specific tells that were audible on real calls.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Item 32 - language lock
# --------------------------------------------------------------------------

_FRENCH_MARKERS = frozenset(
    {
        "je", "vous", "nous", "et", "le", "la", "les", "des", "une", "un",
        "est", "pour", "avec", "que", "qui", "dans", "sur", "pas", "plus",
        "bonjour", "merci", "oui", "non", "votre", "notre", "c'est", "cela",
    }
)
_ENGLISH_MARKERS = frozenset(
    {
        "the", "and", "you", "your", "for", "with", "that", "this", "is",
        "are", "have", "would", "can", "what", "how", "hello", "thanks",
        "yes", "no", "our", "we", "i", "it", "of", "to",
    }
)
# Words that exist in both or are routinely borrowed, so they prove nothing.
_NEUTRAL = frozenset({"ok", "okay", "internet", "sport", "football", "iptv", "week", "box"})


# Scripts the agent cannot read at all. Latin-only marker matching returns no
# tokens for these, so the caller's own language was invisible and the reply
# went out in whichever of the supported languages the transcriber guessed.
_NON_LATIN_SCRIPTS: tuple[tuple[str, str], ...] = (
    ("ar", r"[\u0600-\u06ff\u0750-\u077f]"),
    ("he", r"[\u0590-\u05ff]"),
    ("ru", r"[\u0400-\u04ff]"),
    ("el", r"[\u0370-\u03ff]"),
    ("zh", r"[\u4e00-\u9fff]"),
    ("ja", r"[\u3040-\u30ff]"),
    ("ko", r"[\uac00-\ud7af]"),
)


def detect_script_language(text: str) -> str:
    """Name a non-Latin script when the caller writes in one.

    Darija typed in Arabic script was labelled French, so the agent answered
    confidently in a language the caller had not used. Naming it lets the agent
    ask which supported language to continue in instead of guessing.
    """

    for code, pattern in _NON_LATIN_SCRIPTS:
        if len(re.findall(pattern, text)) >= 3:
            return code
    return ""


def detect_language(text: str) -> str:
    """Return 'fr', 'en', a non-Latin script code, or '' when undecidable.

    A single borrowed word must never look like a language switch. That is what
    produced an English reply in the middle of a French call.
    """

    script = detect_script_language(text)
    if script:
        return script

    tokens = [
        token
        for token in re.findall(r"[a-zà-ÿ']+", text.casefold())
        if token not in _NEUTRAL
    ]
    if not tokens:
        return ""
    french = sum(token in _FRENCH_MARKERS for token in tokens)
    english = sum(token in _ENGLISH_MARKERS for token in tokens)
    if re.search(r"[àâçéèêëîïôùûü]", text.casefold()):
        french += 2
    if french >= 2 and french > english * 2:
        return "fr"
    if english >= 2 and english > french * 2:
        return "en"
    # A very short reply cannot reach a count of two, so "Parfait, merci."
    # read as undecidable and passed the lock during an English call. With no
    # counter-evidence at all, one marker in a short phrase is decisive.
    if len(tokens) <= 4:
        if french and not english:
            return "fr"
        if english and not french:
            return "en"
    return ""


def violates_language_lock(text: str, configured: str) -> bool:
    """True when the reply is decisively in the wrong language."""

    detected = detect_language(text)
    if not detected:
        return False
    expected = "fr" if configured.lower().startswith("fr") else "en"
    return detected != expected


# --------------------------------------------------------------------------
# Item 33 - numbers, prices and dates spoken as words
# --------------------------------------------------------------------------

_FR_UNITS = (
    "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit",
    "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
)
_EN_UNITS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen",
)
_FR_TENS = {
    20: "vingt", 30: "trente", 40: "quarante", 50: "cinquante",
    60: "soixante", 70: "soixante-dix", 80: "quatre-vingts", 90: "quatre-vingt-dix",
}
_EN_TENS = {
    20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
    60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
}


def _small_number_words(value: int, french: bool) -> str:
    units = _FR_UNITS if french else _EN_UNITS
    tens = _FR_TENS if french else _EN_TENS
    if value < len(units):
        return units[value]
    if value < 100:
        ten, unit = divmod(value, 10)
        base = tens.get(ten * 10)
        if base is None:
            return str(value)
        if unit == 0:
            return base
        return f"{base}-{units[unit]}"
    return str(value)


def spoken_numbers(text: str, language: str) -> str:
    """Convert digits a caller would hear read out into spoken words."""

    return normalize_for_speech(text, language)


def normalize_for_speech(text: str, language: str) -> str:
    """Normalize text into natural conversational phonetics for TTS."""
    if not text:
        return ""
    french = language.lower().startswith("fr")

    def expand_currency(m: re.Match[str]) -> str:
        amount_str = m.group("amount").replace(",", ".")
        raw_match = m.group(0).upper()
        curr = (m.group("curr") or "").upper()
        if "€" in raw_match or "EURO" in curr or "EUR" in curr:
            unit_name = "euros"
        elif "$" in raw_match or "USD" in curr or "DOLLAR" in curr:
            unit_name = "dollars"
        elif "MAD" in curr or "DH" in curr or "DIRHAM" in curr:
            unit_name = "dirhams"
        elif "£" in raw_match or "GBP" in curr or "POUND" in curr:
            unit_name = "livres" if french else "pounds"
        else:
            unit_name = "euros"

        if "." in amount_str:
            parts = amount_str.split(".", 1)
            main_val = int(parts[0]) if parts[0].isdigit() else 0
            cents_val = int(parts[1][:2]) if parts[1][:2].isdigit() else 0
            if french:
                main_w = _small_number_words(main_val, french=True)
                cents_w = _small_number_words(cents_val, french=True)
                if cents_val > 0:
                    return f"{main_w} {unit_name} {cents_w}"
                return f"{main_w} {unit_name}"
            else:
                main_w = _small_number_words(main_val, french=False)
                cents_w = _small_number_words(cents_val, french=False)
                if cents_val > 0:
                    return f"{main_w} {unit_name} and {cents_w} cents"
                return f"{main_w} {unit_name}"
        else:
            val = int(amount_str) if amount_str.isdigit() else 0
            val_w = _small_number_words(val, french=french)
            return f"{val_w} {unit_name}"

    # Match currency patterns: 29.99€, 150 €, € 150, 50$, $50, 500 MAD, 500 DH
    text = re.sub(
        r"(?P<amount>\d+(?:[.,]\d{1,2})?)\s*(?P<curr>€|euros?\b|EUR\b|\$|USD\b|dollars?\b|MAD\b|DH\b|dirhams?\b|£|GBP\b)",
        expand_currency,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?P<curr>€|\$|£)\s*(?P<amount>\d+(?:[.,]\d{1,2})?)\b",
        expand_currency,
        text,
        flags=re.IGNORECASE,
    )

    # Expand small plain numbers if < 100
    def plain(match: re.Match[str]) -> str:
        digits = match.group(0)
        if len(digits) > 4:
            return digits
        value = int(digits)
        if value > 99:
            return digits
        return _small_number_words(value, french)

    text = re.sub(r"\b\d+\b", plain, text)

    # Expand common technical/sales acronyms for natural phonetic pronunciation
    acronym_map = {
        r"\bIPTV\b": "I P T V",
        r"\b4K\b": "4 K",
        r"\bFHD\b": "F H D",
        r"\bHD\b": "H D",
        r"\bVOD\b": "V O D",
        r"\bOTT\b": "O T T",
        r"\bVPN\b": "V P N",
        r"\bVIP\b": "V I P",
        r"\bSMS\b": "S M S",
        r"\bPDF\b": "P D F",
        r"\bCRM\b": "C R M",
        r"\bERP\b": "E R P",
        r"\bWiFi\b": "Wi-Fi",
        r"\bURL\b": "U R L",
    }
    for pattern, repl in acronym_map.items():
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    # Expand symbols
    if french:
        text = re.sub(r"(\d+)\s*%", r"\1 pour cent", text)
        text = text.replace("&", " et ")
        text = text.replace("+", " plus ")
    else:
        text = re.sub(r"(\d+)\s*%", r"\1 percent", text)
        text = text.replace("&", " and ")
        text = text.replace("+", " plus ")

    return " ".join(text.split())


# --------------------------------------------------------------------------
# Items 27, 28, 29 - varied acknowledgements, connectors, disfluency
# --------------------------------------------------------------------------

FRENCH_ACKS = (
    "D'accord.", "Très bien.", "Je vois.", "Entendu.", "Ah oui.",
    "Parfait.", "D'accord, je note.", "Compris.",
)
ENGLISH_ACKS = (
    "Right.", "Got it.", "I see.", "Understood.", "Okay.",
    "Perfect.", "Noted.", "Sure.",
)
FRENCH_CONNECTORS = ("Du coup,", "En fait,", "Alors,", "Bon,", "Donc,")
ENGLISH_CONNECTORS = ("So,", "Right,", "Well,", "Actually,", "Okay so,")
FRENCH_FILLERS = ("Alors…", "Voyons…", "Hmm…")
ENGLISH_FILLERS = ("Let me see…", "Right…", "Hmm…")


@dataclass
class VariedPhrasePicker:
    """Emit from a pool without ever repeating consecutively (item 27)."""

    pool: tuple[str, ...]
    _recent: list[str] = field(default_factory=list)
    _rng: random.Random = field(default_factory=random.Random)

    def pick(self) -> str:
        if not self.pool:
            return ""
        options = [item for item in self.pool if item not in self._recent]
        if not options:
            self._recent.clear()
            options = list(self.pool)
        chosen = self._rng.choice(options)
        self._recent.append(chosen)
        if len(self._recent) > max(2, len(self.pool) // 2):
            self._recent.pop(0)
        return chosen


def acknowledgements_for(language: str) -> tuple[str, ...]:
    return FRENCH_ACKS if language.lower().startswith("fr") else ENGLISH_ACKS


def fillers_for(language: str) -> tuple[str, ...]:
    return FRENCH_FILLERS if language.lower().startswith("fr") else ENGLISH_FILLERS


_ACK_PREFIX_RE = re.compile(
    r"^(?:d'accord|très bien|je vois|entendu|compris|parfait|"
    r"right|got it|i see|understood|okay|ok|perfect|noted|sure)\b[\s,.!]*",
    re.IGNORECASE,
)


def strip_leading_acknowledgement(text: str) -> str:
    """Remove a canned opener so a fresh one can be chosen (item 27)."""

    return _ACK_PREFIX_RE.sub("", text, count=1).lstrip()


def apply_contractions(text: str, language: str) -> str:
    """Speak the way people do, not the way documents are written (item 28)."""

    if language.lower().startswith("fr"):
        replacements = (
            (r"\bje ne (\w+) pas\b", r"je \1 pas"),
            (r"\bnous allons\b", "on va"),
            (r"\bnous avons\b", "on a"),
            (r"\bcela\b", "ça"),
            (r"\bil n'y a pas\b", "y a pas"),
        )
    else:
        replacements = (
            (r"\bdo not\b", "don't"),
            (r"\bit is\b", "it's"),
            (r"\byou are\b", "you're"),
            (r"\bI am\b", "I'm"),
            (r"\bwe are\b", "we're"),
            (r"\bthat is\b", "that's"),
            (r"\bcannot\b", "can't"),
            (r"\bwill not\b", "won't"),
        )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text


def add_breathing_pauses(text: str) -> str:
    """Give the synthesizer somewhere to breathe (item 56).

    Long clauses read as one unbroken rush. A comma before a coordinating
    connector is what a speaker would naturally pause at.
    """

    return re.sub(
        r"(\w{4,})\s+(mais|donc|et puis|alors que|but|so then|and then)\s+",
        r"\1, \2 ",
        text,
        flags=re.IGNORECASE,
    )


# --------------------------------------------------------------------------
# Item 30 - register (tu/vous, formal/casual)
# --------------------------------------------------------------------------

_TU_RE = re.compile(r"\b(?:tu|ton|ta|tes|toi)\b", re.IGNORECASE)
_VOUS_RE = re.compile(r"\b(?:vous|votre|vos)\b", re.IGNORECASE)


def detect_register(text: str) -> str:
    """Return 'tu', 'vous', or '' from how the caller addresses us."""

    tu = len(_TU_RE.findall(text))
    vous = len(_VOUS_RE.findall(text))
    if tu and tu > vous:
        return "tu"
    if vous and vous > tu:
        return "vous"
    return ""


# --------------------------------------------------------------------------
# Items 16, 17, 18 - pacing
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PacingDecision:
    """How long to wait, and whether to say something while thinking."""

    delay_secs: float
    filler: str


def decide_pacing(
    *,
    caller_words: int,
    reply_words: int,
    language: str,
    enabled: bool = True,
    filler_threshold_words: int = 14,
) -> PacingDecision:
    """A person answers "oui ou non" instantly and a hard question slower.

    Replying to everything at exactly the same speed is a machine tell in both
    directions: too fast for a considered answer, too slow for a trivial one.
    """

    if not enabled:
        return PacingDecision(delay_secs=0.0, filler="")
    if caller_words <= 3 and reply_words <= 12:
        return PacingDecision(delay_secs=0.0, filler="")
    if reply_words >= filler_threshold_words and caller_words >= 8:
        # A visibly considered answer: a short filler covers the thinking and
        # stops the pause reading as a dropped line.
        return PacingDecision(delay_secs=0.12, filler=fillers_for(language)[0])
    return PacingDecision(delay_secs=0.08, filler="")
