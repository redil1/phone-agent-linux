"""Conversational repair: what a person does when they did not catch something.

The pipeline previously forced every caller turn into a confident answer. A
half-heard word, a cough, or a bare "Hello?" still reached the model, which
produced a fluent reply to something the caller never said. That is the
strongest tell that the voice is a machine: a person asks, a bot guesses.

Turns are classified before they can reach the model, and a turn carrying
nothing to answer produces a short human repair instead. The repair never calls
the language model, so it also arrives faster than a real answer would - which
is how a person reacts to not hearing something.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Bare acknowledgements. A person does not answer these; they keep listening.
_BACKCHANNELS = frozenset(
    {
        "mm", "mmm", "mhm", "mm hmm", "mmhmm", "uh huh", "hmm", "hm", "ah",
        "ok", "okay", "d accord", "daccord", "ouais", "hum", "euh", "eh",
        "right", "i see", "je vois", "voila", "bien", "bon", "ah ok", "ah oui",
        "mm ok", "uh", "er", "ben",
    }
)

# The caller asking *us* to repeat. Answering with new content is the most
# jarring failure of all: they already did not hear the previous turn.
_REPEAT_REQUESTS = frozenset(
    {
        "hein", "quoi", "pardon", "comment", "repetez", "repeter", "redites",
        "excusez moi", "excuse me", "sorry", "what", "again", "huh",
        "say that again", "come again", "vous disiez", "j ai pas entendu",
        "je n ai pas entendu", "i didn t hear", "i didn t catch that",
        "pardon je n ai pas entendu", "can you repeat", "vous pouvez repeter", "hello", "can you send them by whatsapp",
        "comment ça", "quoi donc",
    }
)

# Longer turns can ask for an exact rehearsal while also giving useful coaching.
# They must remain ACTIONABLE so the model hears all of the feedback, but the
# output guard must know that repeating the requested line is intentional.
_REHEARSAL_REQUEST = re.compile(
    r"\b(?:"
    r"(?:try|do|give|run|take|say|read|deliver)\s+"
    r"(?:it\s+again|that\s+again|that\s+line\s+again|the\s+(?:full|whole)\s+"
    r"(?:sentence|line|intro(?:duction)?)|another\s+(?:take|pass)|one\s+more\s+"
    r"(?:take|pass|time))|"
    r"(?:want|would\s+you\s+like)\s+to\s+(?:try|do|give)\s+"
    r"(?:another|one\s+more)\s+(?:take|pass)|"
    r"(?:another|one\s+more)\s+(?:take|pass)|"
    r"(?:essayez|faites|dites|r[eé]p[eé]tez)\s+"
    r"(?:encore|une\s+autre\s+fois|la\s+phrase\s+compl[eè]te)|"
    r"encore\s+une\s+(?:fois|prise)"
    r")\b",
    re.IGNORECASE,
)

# Short answers that only mean something while a question is open.
_SHORT_ANSWERS = frozenset(
    {
        "oui", "non", "si", "yes", "no", "yeah", "nope", "yep", "peut etre",
        "maybe", "sure", "exact", "exactement", "tout a fait", "jamais",
        "toujours", "parfois", "aucun", "none", "both", "les deux",
    }
)

# Caller says now is not the time. Pressing on is what a machine does.
_NOT_NOW = (
    r"(?:je conduis|au volant|en voiture|je suis occup(?:e|ee)?|pas le temps|"
    r"en r[eé]union|au travail|je travaille|rappelez(?:-moi)?|"
    r"rappeler plus tard|pas maintenant|je (?:ne )?peux pas parler|"
    r"ce n est pas le bon moment)",
    r"(?:i'?m driving|i am driving|i'?m busy|i am busy|i have no time|"
    r"in a meeting|i'?m at work|call me back(?: later)?|call back later|"
    r"not now|this is a bad time|it'?s a bad time|now is not a good time|"
    r"this is not a good time|i can'?t talk|i cannot talk)",
)

# These constructions discuss wording or another person's possible response.
# A substring matcher previously read "If they hesitate, offer a text call back
# later" as the caller personally requesting a callback and poisoned the live
# state for the rest of the call.
_REPORTED_OR_HYPOTHETICAL = re.compile(
    r"\b(?:something like|for example|for instance|if (?:they|he|she|someone|"
    r"the caller|the customer)|you (?:could|should|can) say|tell (?:them|him|her)|"
    r"ask (?:them|him|her)|offer (?:them|him|her)|say(?:ing)? that|"
    r"par exemple|quelque chose comme|s['\u2019]il|si (?:la personne|le client|elle))\b",
    re.IGNORECASE,
)

# "Who is this?" / "How did you get my number?"
_IDENTITY_CHALLENGE = (
    r"\b(?:qui (?:est-ce|êtes|etes)|c'?est qui|vous (?:êtes|etes) qui|"
    r"o[uù] avez[- ]vous eu|comment avez[- ]vous eu|d'?o[uù] venez)\b",
    r"\b(?:who is this|who'?s this|who are you|where did you get my number|"
    r"how did you get my number)\b",
)


class TurnQuality(StrEnum):
    ACTIONABLE = "actionable"
    BACKCHANNEL = "backchannel"
    FRAGMENT = "fragment"
    UNINTELLIGIBLE = "unintelligible"
    REPEAT_REQUEST = "repeat_request"
    NOT_NOW = "not_now"
    IDENTITY_CHALLENGE = "identity_challenge"


def normalize(text: str) -> str:
    stripped = re.sub(r"[^\w\sÀ-ÿ']", " ", text.casefold(), flags=re.UNICODE)
    return " ".join(stripped.replace("_", " ").split())


def words_of(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:['\u2019][^\W_]+)?", text, flags=re.UNICODE)


def looks_like_noise(text: str) -> bool:
    """Detect a transcript carrying no recoverable meaning."""

    normalized = normalize(text)
    if not normalized:
        return True
    tokens = words_of(normalized)
    if not tokens:
        return True
    # If the transcript contains 3 or more words, check if it's just a single repeated word like 'beep beep beep'
    if len(tokens) >= 3:
        return len(set(tokens)) == 1
    letters = re.sub(r"[^a-zà-ÿ]", "", normalized)
    if not letters:
        return True
    vowels = sum(character in "aeiouyàâäéèêëîïôöùûü" for character in letters)
    if len(letters) >= 4 and vowels / len(letters) < 0.14:
        return True
    return False


def _matches(patterns: tuple[str, ...], normalized: str) -> bool:
    return any(re.search(pattern, normalized) for pattern in patterns)


def is_direct_not_now(text: str) -> bool:
    """Return true only for a caller's own, direct bad-time statement.

    Bad timing is important enough to stop selling, but it is not safe to infer
    from one phrase embedded in coaching, quotation, or a hypothetical example.
    The accepted form is deliberately a short standalone utterance; longer
    turns remain fully available to the language model instead of being reduced
    to one regex label.
    """

    normalized = normalize(text)
    if not normalized or _REPORTED_OR_HYPOTHETICAL.search(normalized):
        return False
    tokens = words_of(normalized)
    if len(tokens) > 18:
        return False
    wrappers = (
        r"(?:sorry|please|actually|well|look|thanks|thank you|no thanks|"
        r"desole|désolé|bon)?"
    )
    suffix = (
        r"(?:right now|at the moment|today|tomorrow|later today|this afternoon|"
        r"this evening|now|la|là|maintenant|demain|cet apres midi|please|thanks|thank you|"
        r"could you call (?:me )?later|can you call (?:me )?later|"
        r"rappelez moi plus tard|merci)?"
    )
    return any(
        re.fullmatch(rf"{wrappers}\s*{pattern}\s*{suffix}", normalized)
        for pattern in _NOT_NOW
    )


def caller_authorizes_repetition(text: str) -> bool:
    """Whether repeating prior wording is the caller's current request.

    This stays separate from turn classification: a coaching turn such as
    "smooth the rhythm, then try the full sentence" contains meaning beyond a
    bare "say that again", yet both authorize a repeated rehearsal.
    """

    normalized = normalize(text)
    if not normalized:
        return False
    if normalized in _REPEAT_REQUESTS:
        return True
    return bool(_REHEARSAL_REQUEST.search(normalized))


def classify_caller_turn(
    text: str,
    *,
    question_is_open: bool = False,
    language: str = "en-US",
) -> TurnQuality:
    """Decide what kind of thing the caller just said."""

    normalized = normalize(text)
    if not normalized:
        return TurnQuality.UNINTELLIGIBLE
    if any(
        k in normalized
        for k in (
            "whatsapp",
            "message",
            "sms",
            "link",
            "email",
            "envoyer",
            "send",
            "start first",
            "phone",
            "number",
            "hear",
            "catalog",
            "price",
            "prix",
        )
    ):
        return TurnQuality.ACTIONABLE
    if normalized in _REPEAT_REQUESTS:
        return TurnQuality.REPEAT_REQUEST
    if _matches(_IDENTITY_CHALLENGE, normalized):
        return TurnQuality.IDENTITY_CHALLENGE
    if is_direct_not_now(text):
        return TurnQuality.NOT_NOW

    if normalized in _SHORT_ANSWERS:
        return TurnQuality.ACTIONABLE if question_is_open else TurnQuality.BACKCHANNEL
    if normalized in _BACKCHANNELS:
        return TurnQuality.BACKCHANNEL

    if looks_like_noise(text):
        return TurnQuality.UNINTELLIGIBLE

    tokens = words_of(normalized)
    if len(tokens) == 1:
        return TurnQuality.ACTIONABLE if question_is_open else TurnQuality.FRAGMENT
    return TurnQuality.ACTIONABLE


@dataclass(frozen=True, slots=True)
class RepairPhrases:
    """Human wordings for escalating levels of not having heard."""

    first: tuple[str, ...]
    second: tuple[str, ...]
    final: tuple[str, ...]
    repeat_back: tuple[str, ...]
    not_now: tuple[str, ...]
    identity: tuple[str, ...]


FRENCH_PHRASES = RepairPhrases(
    first=(
        "Pardon, je n'ai pas bien saisi. Vous pouvez répéter ?",
        "Excusez-moi, j'ai mal entendu. Vous disiez ?",
        "Désolé, je n'ai pas tout compris. Vous pouvez redire ?",
        "Pardon, la ligne a coupé. Vous pouvez reprendre ?",
    ),
    second=(
        "La ligne coupe un peu. Vous pouvez répéter plus lentement ?",
        "J'ai encore du mal à vous entendre. Reprenez juste la fin ?",
    ),
    final=(
        "La ligne est vraiment mauvaise. Je peux vous rappeler dans un moment ?",
        "Je vous entends très mal. Vous préférez que je rappelle plus tard ?",
    ),
    repeat_back=(
        "Bien sûr, je disais :",
        "Pas de souci, je répète :",
        "Je reformule :",
    ),
    not_now=(
        "Pas de souci, je ne vous dérange pas. Quel moment vous arrangerait ?",
        "Je comprends, j'appelle mal. Je vous rappelle quand ?",
    ),
    identity=(
        "C'est {name}, d'{company}. Je vous appelle au sujet de nos abonnements.",
        "{name}, d'{company}. Je me permets de vous appeler pour nos abonnements.",
    ),
)

ENGLISH_PHRASES = RepairPhrases(
    first=(
        "Sorry, I didn't quite catch that. Could you say it again?",
        "Excuse me, I missed that. What were you saying?",
        "Sorry, I didn't get all of that. Could you repeat it?",
        "Sorry, the line cut out. Could you start again?",
    ),
    second=(
        "The line is breaking up. Could you say that a bit slower?",
        "I'm still having trouble hearing you. Just the last part?",
    ),
    final=(
        "The line is really poor. Could I call you back in a moment?",
        "I can barely hear you. Would you rather I called back later?",
    ),
    repeat_back=(
        "Of course, I was saying:",
        "No problem, let me repeat:",
        "Let me put that another way:",
    ),
    not_now=(
        "No problem at all, I've caught you at a bad time. When would suit you?",
        "I understand, bad timing. When should I call back?",
    ),
    identity=(
        "It's {name}, from {company}. I'm calling about our subscriptions.",
        "{name}, from {company}. I'm calling regarding our subscriptions.",
    ),
)


def phrases_for(language: str) -> RepairPhrases:
    return FRENCH_PHRASES if language.lower().startswith("fr") else ENGLISH_PHRASES


@dataclass
class RepairPolicy:
    """Track consecutive misses and pick wording that never repeats."""

    language: str = "en-US"
    overrides: dict[str, Any] = field(default_factory=dict)
    consecutive_failures: int = 0
    _recent: list[str] = field(default_factory=list)
    _rng: random.Random = field(default_factory=random.Random)

    def _pool(self, level: str) -> tuple[str, ...]:
        configured = self.overrides.get(level)
        if isinstance(configured, list):
            cleaned = tuple(str(item).strip() for item in configured if str(item).strip())
            if cleaned:
                return cleaned
        return getattr(phrases_for(self.language), level)

    def _pick(self, level: str, *, avoid: tuple[str, ...] = ()) -> str:
        pool = self._pool(level)
        if not pool:
            return ""
        # Hearing the identical apology twice is what makes repair sound
        # automated, so recently used wordings are excluded while alternatives
        # remain.
        avoided = {normalize(phrase) for phrase in avoid if normalize(phrase)}
        options = [
            phrase
            for phrase in pool
            if phrase not in self._recent and normalize(phrase) not in avoided
        ]
        if not options:
            options = [phrase for phrase in pool if normalize(phrase) not in avoided]
        if not options:
            # A persona may configure only one repair sentence. Repeating a
            # safe repair after two failed model drafts is still preferable to
            # dead air, and the response policy marks this exceptional path.
            options = list(pool)
        chosen = self._rng.choice(options)
        self._recent.append(chosen)
        if len(self._recent) > max(2, len(pool) - 1):
            self._recent.pop(0)
        return chosen

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def next_repair(self, *, avoid: tuple[str, ...] = ()) -> str:
        """Escalate the way a person does instead of repeating one apology."""

        self.consecutive_failures += 1
        if self.consecutive_failures == 1:
            return self._pick("first", avoid=avoid)
        if self.consecutive_failures == 2:
            return self._pick("second", avoid=avoid)
        return self._pick("final", avoid=avoid)

    def repeat_preamble(self) -> str:
        return self._pick("repeat_back")

    def not_now_reply(self) -> str:
        return self._pick("not_now")

    def identity_reply(self, *, name: str, company: str) -> str:
        template = self._pick("identity")
        return template.replace("{name}", name).replace("{company}", company)

    def should_hand_off(self) -> bool:
        """Three misses in a row is where a person stops pushing."""

        return self.consecutive_failures >= 3
