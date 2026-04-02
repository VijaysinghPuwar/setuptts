"""Text cleanup, language heuristics, and voice compatibility checks for TTS."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_ELLIPSIS_RE = re.compile(r"(?:\.{4,}|…+)")
_REPEATED_PUNCT_RE = re.compile(r"([!?])\1{2,}")
_MARKDOWN_BULLET_RE = re.compile(r"(?m)^[ \t]*[-*]\s+")
_UNICODE_BULLET_RE = re.compile(r"[•▪■●◦◆◇▶►▸▹➜➤➝]+")
_NOISY_DECORATION_RE = re.compile(r"[※★☆✦✧✩✪✫✬✭✮✯❖❥♡♥♦♣♠]+")
_EXTRA_DASH_RE = re.compile(r"[‐‑‒–—―]{2,}")
_MULTISPACE_RE = re.compile(r"[ \t\f\v]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([(\[{])\s+")
_SPACE_BEFORE_CLOSE_RE = re.compile(r"\s+([)\]}])")
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

_PUNCT_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u2035": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\u2036": '"',
        "\u2039": "<",
        "\u203a": ">",
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2043": "-",
        "\u00b7": ",",
        "\u2026": "...",
        "\u00a0": " ",
        "\u3000": " ",
        "\u3008": "<",
        "\u3009": ">",
        "\u300a": "<<",
        "\u300b": ">>",
        "\u300c": '"',
        "\u300d": '"',
        "\u300e": '"',
        "\u300f": '"',
        "\u3010": "[",
        "\u3011": "]",
        "\u3014": "[",
        "\u3015": "]",
        "\u3016": "[",
        "\u3017": "]",
        "\u3018": "[",
        "\u3019": "]",
        "\u301a": "[",
        "\u301b": "]",
        "\uff08": "(",
        "\uff09": ")",
        "\uff3b": "[",
        "\uff3d": "]",
        "\uff5b": "{",
        "\uff5d": "}",
    }
)

_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "latin": (
        (0x0041, 0x005A),
        (0x0061, 0x007A),
        (0x00C0, 0x00FF),
        (0x0100, 0x024F),
        (0x1E00, 0x1EFF),
    ),
    "devanagari": ((0x0900, 0x097F), (0xA8E0, 0xA8FF)),
    "arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF)),
    "cyrillic": ((0x0400, 0x04FF), (0x0500, 0x052F)),
    "han": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF)),
    "hiragana": ((0x3040, 0x309F),),
    "katakana": ((0x30A0, 0x30FF), (0x31F0, 0x31FF)),
    "hangul": ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)),
    "thai": ((0x0E00, 0x0E7F),),
    "hebrew": ((0x0590, 0x05FF),),
    "greek": ((0x0370, 0x03FF),),
    "bengali": ((0x0980, 0x09FF),),
    "gurmukhi": ((0x0A00, 0x0A7F),),
    "gujarati": ((0x0A80, 0x0AFF),),
    "tamil": ((0x0B80, 0x0BFF),),
    "telugu": ((0x0C00, 0x0C7F),),
    "kannada": ((0x0C80, 0x0CFF),),
    "malayalam": ((0x0D00, 0x0D7F),),
    "odia": ((0x0B00, 0x0B7F),),
    "sinhala": ((0x0D80, 0x0DFF),),
    "lao": ((0x0E80, 0x0EFF),),
    "myanmar": ((0x1000, 0x109F), (0xA9E0, 0xA9FF)),
    "khmer": ((0x1780, 0x17FF),),
    "georgian": ((0x10A0, 0x10FF), (0x2D00, 0x2D2F)),
    "armenian": ((0x0530, 0x058F),),
}

_SCRIPT_LABELS = {
    "latin": "Latin script",
    "devanagari": "Devanagari script",
    "arabic": "Arabic script",
    "cyrillic": "Cyrillic script",
    "han": "Chinese text",
    "japanese": "Japanese text",
    "hangul": "Korean text",
    "thai": "Thai text",
    "hebrew": "Hebrew text",
    "greek": "Greek text",
    "bengali": "Bengali script",
    "gurmukhi": "Gurmukhi script",
    "gujarati": "Gujarati script",
    "tamil": "Tamil text",
    "telugu": "Telugu text",
    "kannada": "Kannada text",
    "malayalam": "Malayalam text",
    "odia": "Odia text",
    "sinhala": "Sinhala text",
    "lao": "Lao text",
    "myanmar": "Burmese text",
    "khmer": "Khmer text",
    "georgian": "Georgian text",
    "armenian": "Armenian text",
    "mixed": "mixed-language text",
}

_LANGUAGE_NAMES = {
    "af": "Afrikaans",
    "am": "Amharic",
    "ar": "Arabic",
    "az": "Azerbaijani",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "bs": "Bosnian",
    "ca": "Catalan",
    "cs": "Czech",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "eu": "Basque",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "ga": "Irish",
    "gl": "Galician",
    "gu": "Gujarati",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "hy": "Armenian",
    "id": "Indonesian",
    "is": "Icelandic",
    "it": "Italian",
    "ja": "Japanese",
    "ka": "Georgian",
    "kk": "Kazakh",
    "km": "Khmer",
    "kn": "Kannada",
    "ko": "Korean",
    "lo": "Lao",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mk": "Macedonian",
    "ml": "Malayalam",
    "mn": "Mongolian",
    "mr": "Marathi",
    "ms": "Malay",
    "mt": "Maltese",
    "my": "Burmese",
    "nb": "Norwegian",
    "ne": "Nepali",
    "nl": "Dutch",
    "or": "Odia",
    "pa": "Punjabi",
    "pl": "Polish",
    "ps": "Pashto",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sq": "Albanian",
    "sr": "Serbian",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "wuu": "Shanghainese",
    "yue": "Cantonese",
    "zh": "Chinese",
    "zu": "Zulu",
}

_DEFAULT_LOCALE_BY_LANGUAGE = {
    "ar": "ar-SA",
    "bn": "bn-IN",
    "de": "de-DE",
    "en": "en-US",
    "es": "es-ES",
    "fa": "fa-IR",
    "fr": "fr-FR",
    "gu": "gu-IN",
    "he": "he-IL",
    "hi": "hi-IN",
    "is": "is-IS",
    "it": "it-IT",
    "ja": "ja-JP",
    "kn": "kn-IN",
    "ko": "ko-KR",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "nb": "nb-NO",
    "ne": "ne-NP",
    "nl": "nl-NL",
    "or": "or-IN",
    "pa": "pa-IN",
    "pl": "pl-PL",
    "pt": "pt-BR",
    "ru": "ru-RU",
    "sv": "sv-SE",
    "ta": "ta-IN",
    "te": "te-IN",
    "th": "th-TH",
    "tr": "tr-TR",
    "uk": "uk-UA",
    "ur": "ur-PK",
    "vi": "vi-VN",
    "zh": "zh-CN",
}

_LANGUAGE_SCRIPTS = {
    "af": "latin",
    "am": "ethiopic",
    "ar": "arabic",
    "az": "latin",
    "bg": "cyrillic",
    "bn": "bengali",
    "bs": "latin",
    "ca": "latin",
    "cs": "latin",
    "cy": "latin",
    "da": "latin",
    "de": "latin",
    "el": "greek",
    "en": "latin",
    "es": "latin",
    "et": "latin",
    "eu": "latin",
    "fa": "arabic",
    "fi": "latin",
    "fil": "latin",
    "fr": "latin",
    "ga": "latin",
    "gl": "latin",
    "gu": "gujarati",
    "he": "hebrew",
    "hi": "devanagari",
    "hr": "latin",
    "hu": "latin",
    "hy": "armenian",
    "id": "latin",
    "is": "latin",
    "it": "latin",
    "ja": "japanese",
    "jv": "latin",
    "ka": "georgian",
    "kk": "cyrillic",
    "km": "khmer",
    "kn": "kannada",
    "ko": "hangul",
    "lo": "lao",
    "lt": "latin",
    "lv": "latin",
    "mk": "cyrillic",
    "ml": "malayalam",
    "mn": "cyrillic",
    "mr": "devanagari",
    "ms": "latin",
    "mt": "latin",
    "my": "myanmar",
    "nb": "latin",
    "ne": "devanagari",
    "nl": "latin",
    "or": "odia",
    "pa": "gurmukhi",
    "pl": "latin",
    "ps": "arabic",
    "pt": "latin",
    "ro": "latin",
    "ru": "cyrillic",
    "si": "sinhala",
    "sk": "latin",
    "sl": "latin",
    "sq": "latin",
    "sr": "cyrillic",
    "sv": "latin",
    "sw": "latin",
    "ta": "tamil",
    "te": "telugu",
    "th": "thai",
    "tr": "latin",
    "uk": "cyrillic",
    "ur": "arabic",
    "uz": "latin",
    "vi": "latin",
    "wuu": "han",
    "yue": "han",
    "zh": "han",
    "zu": "latin",
}

_LATIN_LANGUAGE_HINTS = {
    "en": {"the", "and", "is", "are", "to", "of", "in", "that", "you", "for", "with", "this", "it", "on", "be", "as", "was", "at"},
    "is": {"og", "að", "er", "í", "ekki", "ég", "þú", "fyrir", "sem", "það", "við", "með", "vera"},
    "es": {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una", "para", "con", "por", "del"},
    "fr": {"le", "la", "les", "de", "des", "et", "en", "un", "une", "pour", "avec", "que", "est"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "ich", "zu", "ein", "eine", "mit", "auf", "für"},
    "it": {"il", "lo", "la", "gli", "le", "e", "di", "che", "un", "una", "per", "con", "non"},
    "pt": {"o", "a", "os", "as", "de", "que", "e", "um", "uma", "para", "com", "não", "por"},
    "nl": {"de", "het", "een", "en", "van", "ik", "je", "niet", "voor", "met", "dat", "is"},
    "id": {"dan", "yang", "di", "ini", "untuk", "dengan", "tidak", "dari", "anda", "saya", "itu"},
    "tr": {"ve", "bir", "bu", "için", "ile", "de", "da", "çok", "ama", "olarak", "ben", "sen"},
}

_NON_LATIN_LANGUAGE_HINTS = {
    "hi": {"है", "और", "नहीं", "यह", "आप", "मैं", "में", "के", "का", "की", "से"},
    "mr": {"आहे", "आणि", "नाही", "हे", "मी", "तुम्ही", "च्या", "साठी"},
    "ne": {"तपाईं", "हुनुहुन्छ", "रहेको", "छैन", "यहाँ", "किनभने"},
    "bn": {"এবং", "এই", "আমি", "আপনি", "হয়", "না", "কি", "এর"},
    "ar": {"هذا", "هذه", "أنا", "أنت", "في", "من", "على", "لكن", "ليس"},
    "ru": {"это", "что", "как", "для", "она", "они", "если", "есть"},
    "uk": {"це", "що", "для", "вона", "вони", "якщо", "будь", "є"},
}


@dataclass(frozen=True)
class TextProfile:
    cleaned_text: str
    language_code: str | None
    language_name: str | None
    script_code: str | None
    script_name: str | None
    locale_hint: str | None
    confidence: float
    reason: str

    @property
    def detected_label(self) -> str:
        if self.language_name:
            return self.language_name
        if self.script_name:
            return self.script_name
        return "the current text"


@dataclass(frozen=True)
class VoiceCompatibilityAssessment:
    severity: str
    message: str
    short_message: str
    recommended_voice: str | None
    recommended_label: str | None
    selected_label: str
    selected_locale: str
    profile: TextProfile

    @property
    def requires_confirmation(self) -> bool:
        return self.severity != "ok"


def normalize_text_for_tts(text: str) -> str:
    """Clean punctuation and noisy unicode without changing the meaning."""
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCT_TRANSLATION)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = text.replace("\ufffd", " ")
    text = _MARKDOWN_BULLET_RE.sub("", text)
    text = _UNICODE_BULLET_RE.sub(", ", text)
    text = _NOISY_DECORATION_RE.sub(" ", text)
    text = _ELLIPSIS_RE.sub("...", text)
    text = _REPEATED_PUNCT_RE.sub(lambda m: m.group(1) * 2, text)
    text = _EXTRA_DASH_RE.sub(" - ", text)

    cleaned_chars: list[str] = []
    for char in text:
        if char in {"\n", "\t"}:
            cleaned_chars.append(char)
            continue

        category = unicodedata.category(char)
        if category.startswith("C"):
            cleaned_chars.append(" ")
            continue
        if category == "So":
            cleaned_chars.append(" ")
            continue
        cleaned_chars.append(char)

    lines: list[str] = []
    for raw_line in "".join(cleaned_chars).split("\n"):
        line = _MULTISPACE_RE.sub(" ", raw_line).strip()
        line = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", line)
        line = _SPACE_AFTER_OPEN_RE.sub(r"\1", line)
        line = _SPACE_BEFORE_CLOSE_RE.sub(r"\1", line)
        lines.append(line)

    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if not line:
            blank_run += 1
            if blank_run <= 2:
                collapsed.append("")
            continue
        blank_run = 0
        collapsed.append(line)

    return "\n".join(collapsed).strip()


def build_text_profile(text: str) -> TextProfile:
    """Return cleaned text plus a best-effort language/script guess."""
    cleaned = normalize_text_for_tts(text)
    if not cleaned:
        return TextProfile(
            cleaned_text="",
            language_code=None,
            language_name=None,
            script_code=None,
            script_name=None,
            locale_hint=None,
            confidence=0.0,
            reason="No readable text detected.",
        )

    script_counts = _script_counts(cleaned)
    dominant_script, dominant_count = _dominant_script(script_counts)
    letter_total = sum(script_counts.values())

    if letter_total <= 0:
        return TextProfile(
            cleaned_text=cleaned,
            language_code=None,
            language_name=None,
            script_code=None,
            script_name=None,
            locale_hint=None,
            confidence=0.0,
            reason="The text does not contain enough alphabetic content.",
        )

    if script_counts.get("hiragana", 0) or script_counts.get("katakana", 0):
        dominant_script = "japanese"
        dominant_count = (
            script_counts.get("hiragana", 0)
            + script_counts.get("katakana", 0)
            + script_counts.get("han", 0)
        )

    share = dominant_count / max(letter_total, 1)
    if share < 0.55 and len([count for count in script_counts.values() if count >= 4]) >= 2:
        return TextProfile(
            cleaned_text=cleaned,
            language_code=None,
            language_name=None,
            script_code="mixed",
            script_name=_SCRIPT_LABELS["mixed"],
            locale_hint=None,
            confidence=share,
            reason="Multiple scripts appear throughout the text.",
        )

    tokens = [token.lower() for token in _TOKEN_RE.findall(cleaned)]
    language_code, confidence, reason = _guess_language(tokens, cleaned, dominant_script, share)
    language_name = _LANGUAGE_NAMES.get(language_code) if language_code else None
    locale_hint = _DEFAULT_LOCALE_BY_LANGUAGE.get(language_code) if language_code else None

    if not language_name:
        script_name = _SCRIPT_LABELS.get(dominant_script, dominant_script)
        return TextProfile(
            cleaned_text=cleaned,
            language_code=None,
            language_name=None,
            script_code=dominant_script,
            script_name=script_name,
            locale_hint=None,
            confidence=share,
            reason=reason or f"The text mainly uses {script_name.lower()}.",
        )

    return TextProfile(
        cleaned_text=cleaned,
        language_code=language_code,
        language_name=language_name,
        script_code=_LANGUAGE_SCRIPTS.get(language_code, dominant_script),
        script_name=language_name,
        locale_hint=locale_hint,
        confidence=confidence,
        reason=reason,
    )


def assess_voice_compatibility(
    profile: TextProfile,
    selected_voice: str,
    voices: list[Any],
) -> VoiceCompatibilityAssessment:
    """Decide whether the chosen voice is a good fit for the current text."""
    voice = _find_voice(voices, selected_voice)
    selected_locale = _voice_locale(voice if voice is not None else selected_voice)
    selected_language = _voice_language_code(selected_locale)
    selected_script = _LANGUAGE_SCRIPTS.get(selected_language)
    selected_label = _voice_language_label(voice, selected_locale)
    recommended_voice = recommend_voice(profile, voices, exclude=selected_voice, preferred_gender=_voice_gender(voice))
    recommended_label = _voice_language_label(_find_voice(voices, recommended_voice), _voice_locale(recommended_voice)) if recommended_voice else None

    if not profile.cleaned_text or not profile.script_code:
        return VoiceCompatibilityAssessment(
            severity="ok",
            message="",
            short_message="",
            recommended_voice=None,
            recommended_label=None,
            selected_label=selected_label,
            selected_locale=selected_locale,
            profile=profile,
        )

    multilingual = "multilingual" in selected_voice.lower()
    same_language = bool(profile.language_code and selected_language == profile.language_code)
    same_script = bool(selected_script and selected_script == profile.script_code)

    if same_language:
        return VoiceCompatibilityAssessment(
            severity="ok",
            message="",
            short_message="",
            recommended_voice=None,
            recommended_label=None,
            selected_label=selected_label,
            selected_locale=selected_locale,
            profile=profile,
        )

    if profile.language_name and not same_language:
        severity = "warning" if multilingual and same_script else "mismatch"
        message = (
            f"This text appears to be {profile.language_name}, but the selected voice is {selected_label}.\n\n"
            "This voice may produce poor pronunciation for the current text."
        )
        return VoiceCompatibilityAssessment(
            severity=severity,
            message=_append_recommendation(message, recommended_voice),
            short_message="Voice/text mismatch detected.",
            recommended_voice=recommended_voice,
            recommended_label=recommended_label,
            selected_label=selected_label,
            selected_locale=selected_locale,
            profile=profile,
        )

    if not same_script:
        script_label = profile.script_name or "the detected script"
        message = (
            f"This text appears to use {script_label.lower()}, but the selected voice is {selected_label}.\n\n"
            "This voice is likely to produce poor pronunciation or gibberish."
        )
        return VoiceCompatibilityAssessment(
            severity="mismatch",
            message=_append_recommendation(message, recommended_voice),
            short_message="Voice/script mismatch detected.",
            recommended_voice=recommended_voice,
            recommended_label=recommended_label,
            selected_label=selected_label,
            selected_locale=selected_locale,
            profile=profile,
        )

    return VoiceCompatibilityAssessment(
        severity="ok",
        message="",
        short_message="",
        recommended_voice=None,
        recommended_label=None,
        selected_label=selected_label,
        selected_locale=selected_locale,
        profile=profile,
    )


def recommend_voice(
    profile: TextProfile,
    voices: list[Any],
    *,
    exclude: str | None = None,
    preferred_gender: str | None = None,
) -> str | None:
    """Return the best available voice for the detected text profile."""
    if not profile.cleaned_text:
        return None

    candidates = [voice for voice in voices if _voice_short_name(voice) != exclude]
    if not candidates:
        return None

    if profile.locale_hint:
        exact = [voice for voice in candidates if _voice_locale(voice) == profile.locale_hint]
        chosen = _pick_preferred_voice(exact, preferred_gender)
        if chosen:
            return _voice_short_name(chosen)

    if profile.language_code:
        same_language = [
            voice for voice in candidates
            if _voice_language_code(_voice_locale(voice)) == profile.language_code
        ]
        chosen = _pick_preferred_voice(same_language, preferred_gender)
        if chosen:
            return _voice_short_name(chosen)

    if profile.script_code:
        same_script = [
            voice for voice in candidates
            if _LANGUAGE_SCRIPTS.get(_voice_language_code(_voice_locale(voice))) == profile.script_code
        ]
        chosen = _pick_preferred_voice(same_script, preferred_gender)
        if chosen:
            return _voice_short_name(chosen)

    return None


def _append_recommendation(message: str, recommended_voice: str | None) -> str:
    if not recommended_voice:
        return message
    return f"{message}\nRecommended voice: {recommended_voice}"


def _script_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for char in text:
        script = _char_script(char)
        if script:
            counts[script] = counts.get(script, 0) + 1
    return counts


def _dominant_script(script_counts: dict[str, int]) -> tuple[str | None, int]:
    if not script_counts:
        return None, 0
    return max(script_counts.items(), key=lambda item: item[1])


def _guess_language(
    tokens: list[str],
    cleaned: str,
    dominant_script: str | None,
    share: float,
) -> tuple[str | None, float, str]:
    if not dominant_script:
        return None, 0.0, "No dominant writing system detected."

    if dominant_script == "japanese":
        return "ja", min(0.92, share + 0.1), "The text includes hiragana/katakana, which strongly indicates Japanese."

    if dominant_script == "hangul":
        return "ko", min(0.92, share + 0.08), "The text is predominantly Hangul."

    if dominant_script == "han":
        return "zh", min(0.88, share + 0.05), "The text is predominantly Chinese Han characters."

    scores: dict[str, float] = {}
    if dominant_script == "latin":
        for language, hints in _LATIN_LANGUAGE_HINTS.items():
            hit_count = sum(1 for token in tokens if token in hints)
            if hit_count:
                scores[language] = hit_count
        icelandic_chars = sum(cleaned.lower().count(char) for char in ("þ", "ð", "æ", "ö"))
        if icelandic_chars:
            scores["is"] = scores.get("is", 0.0) + (icelandic_chars * 0.8)
    else:
        for language, hints in _NON_LATIN_LANGUAGE_HINTS.items():
            lang_script = _LANGUAGE_SCRIPTS.get(language)
            if lang_script != dominant_script and not (
                dominant_script == "devanagari" and language in {"hi", "mr", "ne"}
            ):
                continue
            hit_count = sum(1 for token in tokens if token in hints)
            if hit_count:
                scores[language] = float(hit_count)

        if dominant_script == "cyrillic":
            if any(letter in cleaned for letter in ("і", "ї", "є", "ґ")):
                scores["uk"] = scores.get("uk", 0.0) + 1.5
            if any(letter in cleaned for letter in ("ё", "ы", "э", "ъ")):
                scores["ru"] = scores.get("ru", 0.0) + 1.5

    if scores:
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        language, top_score = ordered[0]
        second_score = ordered[1][1] if len(ordered) > 1 else 0.0
        if top_score >= 2.0 or top_score >= second_score + 1.0:
            confidence = min(0.96, 0.55 + (top_score * 0.08) + (share * 0.15))
            return (
                language,
                confidence,
                f"Common {_LANGUAGE_NAMES.get(language, language).lower()} words were detected in the text.",
            )

    if dominant_script in {"devanagari", "arabic", "bengali", "gurmukhi", "gujarati", "tamil", "telugu", "kannada", "malayalam", "odia", "sinhala", "thai", "hebrew", "greek", "lao", "myanmar", "khmer", "georgian", "armenian"}:
        for language, script in _LANGUAGE_SCRIPTS.items():
            if script == dominant_script and language in _DEFAULT_LOCALE_BY_LANGUAGE:
                confidence = min(0.8, 0.52 + (share * 0.22))
                return (
                    language,
                    confidence,
                    f"The text is predominantly {_SCRIPT_LABELS.get(dominant_script, dominant_script).lower()}.",
                )

    return None, share, f"The text mainly uses {_SCRIPT_LABELS.get(dominant_script, dominant_script).lower()}."


def _char_script(char: str) -> str | None:
    code = ord(char)
    for script, ranges in _SCRIPT_RANGES.items():
        for start, end in ranges:
            if start <= code <= end:
                return script
    return None


def _find_voice(voices: list[Any], short_name: str | None) -> Any | None:
    if not short_name:
        return None
    return next((voice for voice in voices if _voice_short_name(voice) == short_name), None)


def _pick_preferred_voice(voices: list[Any], preferred_gender: str | None) -> Any | None:
    if not voices:
        return None

    def sort_key(voice: Any) -> tuple[int, int, str]:
        gender = (_voice_gender(voice) or "").lower()
        gender_match = 0 if preferred_gender and gender == preferred_gender.lower() else 1
        multilingual_penalty = 0 if "multilingual" in _voice_short_name(voice).lower() else 1
        return (gender_match, multilingual_penalty, _voice_short_name(voice))

    return sorted(voices, key=sort_key)[0]


def _voice_short_name(voice: Any) -> str:
    if voice is None:
        return ""
    if isinstance(voice, dict):
        return voice.get("ShortName", "")
    return getattr(voice, "short_name", "")


def _voice_locale(voice: Any) -> str:
    if voice is None:
        return ""
    if isinstance(voice, str):
        parts = voice.split("-")
        return "-".join(parts[:2]) if len(parts) >= 2 else voice
    if isinstance(voice, dict):
        locale = voice.get("Locale")
        if locale:
            return locale
        return _voice_locale(voice.get("ShortName", ""))
    locale = getattr(voice, "locale", "")
    return locale or _voice_locale(getattr(voice, "short_name", ""))


def _voice_gender(voice: Any) -> str | None:
    if voice is None:
        return None
    if isinstance(voice, dict):
        return voice.get("Gender")
    return getattr(voice, "gender", None)


def _voice_language_code(locale: str) -> str:
    if not locale:
        return ""
    return locale.split("-")[0].lower()


def _voice_language_label(voice: Any, locale: str) -> str:
    language_name = _LANGUAGE_NAMES.get(_voice_language_code(locale), locale or "the selected locale")
    return f"{language_name} ({locale})" if locale else language_name
