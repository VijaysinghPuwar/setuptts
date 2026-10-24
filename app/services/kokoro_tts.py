"""
Offline HD voices (Kokoro-82M, https://huggingface.co/hexgrad/Kokoro-82M).

Kokoro is an 82-million-parameter neural voice model (Apache-2.0) that sounds
far closer to a human narrator than Piper, at roughly 6x realtime on a laptop
processor.  It runs as ONNX on the same onnxruntime and espeak-ng that Piper
already uses, so it adds no packages to the app and behaves the same on
macOS and Windows.

One model file (~325 MB, shared by every voice) and a small style file per
voice (~0.5 MB) are downloaded from Get Voices, pinned to a fixed revision
and verified by SHA-256.

ShortNames look like ``kokoro:af_heart``: the first letter of the key is the
language, the second the gender.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.services import piper_tts
from app.utils.file_ops import replace_with_retry
from app.utils.paths import AppPaths

logger = logging.getLogger(__name__)

KOKORO_PREFIX = "kokoro:"
#: The best-rated voice (grade A in the model's voice list).
DEFAULT_VOICE = "af_heart"
SAMPLE_RATE = 24_000

REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
REVISION = "1939ad2a8e416c0acfeecc08a694d14ef25f2231"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}"
#: Full-precision weights.  The fp16 build is half the size and as fast, but
#: overflows: 20 of 88 book passages came back as all-NaN audio (silence),
#: none with these.  The int8 build is half as fast.
MODEL_FILE = "model.onnx"
MODEL_SHA256 = "8fbea51ea711f2af382e88c833d9e288c6dc82ce5e98421ea61c058ce21a34cb"
MODEL_SIZE = 325_532_232
VOICE_SIZE = 522_240             # 510 rows x 256 float32
_STYLE_ROWS = 510
#: Longest phoneme run the model accepts (its context is 512 with the pads).
MAX_TOKENS = 510

#: language letter -> (espeak-ng voice, locale, language label)
LANGUAGES = {
    "a": ("en-us", "en-US", "English (United States)"),
    "b": ("en", "en-GB", "English (United Kingdom)"),
    "e": ("es", "es-ES", "Spanish (Spain)"),
    "f": ("fr", "fr-FR", "French (France)"),
    "h": ("hi", "hi-IN", "Hindi (India)"),
    "i": ("it", "it-IT", "Italian (Italy)"),
    "p": ("pt-br", "pt-BR", "Portuguese (Brazil)"),
}

#: key -> (SHA-256 of voices/<key>.bin, grade from the model's VOICES.md or "").
#: Japanese and Chinese voices are left out: they need misaki's Japanese and
#: Chinese pronunciation packages, and espeak-ng alone makes them garble.
VOICES = {
    "af_heart": ("d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b", "A"),
    "af_bella": ("f69d836209b78eb8c66e75e3cda491e26ea838a3674257e9d4e5703cbaf55c8b", "A-"),
    "af_nicole": ("cd2191ab31b914ed7b318416b0e4440fdf392ddad9106a060819aa600a64f59a", "B-"),
    "af_aoede": ("4a004c33430762e2461eedb2013fad808ef4ab3121f5300f554476caf58d8361", "C+"),
    "af_kore": ("9be5221b6a941c04b561959b8ff0b06e809444dcc4ab7e75a7b23606f691819e", "C+"),
    "af_sarah": ("4409fbc125afabacc615d94db5398d847006a737b0247d6892b7a9a0007a2f0a", "C+"),
    "af_alloy": ("c4a6b876047fd7fb472edf4ebd63cfac7c3b958a7cae7c106e8f038ca6308c45", "C"),
    "af_nova": ("18778272caa0d0eebaea251c35fd635f038434f9eee5e691d02a174bd328414f", "C"),
    "af_sky": ("4435255c9744f3f31659e0d714ab7689bf65d9e77ec1cce060f083912614f0b9", "C-"),
    "af_jessica": ("a240a5e3c15b43563d6e923bdca8ef5613a23471d9b77653694012435df23bd8", "D"),
    "af_river": ("00a2bcf82b1d86e8f19902ede58c65ccf6c0e43b44b7d74fad54e5d8933c9c30", "D"),
    "am_fenrir": ("c27989f741f7ee34d273a39d8a595cc0837d35f5ced9a29b7cc162614616df43", "C+"),
    "am_michael": ("1d1f21dd8da39c30705cd4c75d039d265e9bc4a2a93ed09bc9e1b1225eb95ba1", "C+"),
    "am_puck": ("fcf73c989033e9233e0b98713eca600c8c74dcc1614b37009d5450ff4a2274a0", "C+"),
    "am_echo": ("3968b92c3c4cd1c4416dbded36c13eaa388a90d5788d02a13e4d781f5f8cf3c3", "D"),
    "am_eric": ("e8b5be17edd1e3636901ce7598baafe2dc8dd8ff707a0c23bf9e461add7e2832", "D"),
    "am_liam": ("52403be32fd047c6a44517cb0bcd6b134f2a18baa73e70ef41651e0eab921ade", "D"),
    "am_onyx": ("da5d135b424164916d75a68ffb4c2abce3d7d5ccc82dd1ee6cf447ce286145e6", "D"),
    "am_santa": ("61150cf726ab6c5ed7a99f90a304f91f5a72c00c592e89ec94e5df11c319227a", "D-"),
    "am_adam": ("162b035ed91cfc48b6046982184c645f72edcdd1b82843347f605d7bf7b15716", "F+"),
    "bf_emma": ("669fe0647f9dd04fcab92f1439a40eeb4c8b4ab1f82e4996fe3d918ce4a63b73", "B-"),
    "bf_isabella": ("3754352c4aaa46d17f27654ab7518d65b62ad6163a0f55a5f4330c2da2c4e94f", "C"),
    "bf_alice": ("08afa6ba24da61ea5e8efa139e5aadc938d83f0a6da5a900adaf763ac1da5573", "D"),
    "bf_lily": ("5e0ee32ebe64a467124976b14e69590746f1c4ce41a12b587a50c862edfea335", "D"),
    "bm_fable": ("f889083196807b4adb15e9204252165f503b8d33d3982e681c52443c49d798f1", "C"),
    "bm_george": ("c4b235a4c1f2cd3b939fed08b899ce9385638b763f7b73a59616c4fc9bd6c9bc", "C"),
    "bm_lewis": ("b8f671cef828c30e66fdf0b0756a76bba58f6bb3398cbbf27058642acbcedb97", "D+"),
    "bm_daniel": ("6b3194bbceffb746733cbc22c8f593dd44e401a71d53895a2dca891bc595a1e8", "D"),
    "ef_dora": ("f66ec66bd295acb18372e37008533a9a3228483ccd294e7538d5d9294ac9a532", ""),
    "em_alex": ("27809e9eafdcbcfff90a3016c697568676531de2a2c39cee29c96c7bd6b83e95", ""),
    "em_santa": ("ad43b774e1ca24d05c6161297d8aeb770ac3d29bb95daf516727af5f7d543683", ""),
    "ff_siwis": ("a35f5675ad08948e326ae75fd0ea16ba5d0042e4f76b5f3d1df77d0a48c54861", "B-"),
    "hf_alpha": ("040be6a4425411cc01fda5fd06693c76bfa78572632852bc8cda9c99232ffb56", "C"),
    "hf_beta": ("cd83ae0bb9b2e4e4fb92b4973bd8d1822ca0036d3c498bf4fc89aa8e33917cc7", "C"),
    "hm_omega": ("b02d9222d9ed00ce26b302173a862c2c93f96cc40b5c422b8d14910b9ff34137", "C"),
    "hm_psi": ("644daf88ba8aeb7bd08950bbdcd4453bb280864e49dc4df93fabc6be32e03f37", "C"),
    "if_sara": ("409b69248798fcdc2542330c76953d230710f19b057e59cb82fdc3c4cf71265c", "C"),
    "im_nicola": ("bc578e510d52a96d6940d46f12e96d7b3df00905dbea075113226d100e6e1ab0", "C"),
    "pf_dora": ("3da7b5b2d91847ebf5646f57631af6ececae3c29a89cd300f06edf9aa6cfe9ee", ""),
    "pm_alex": ("0175c753f59c54e7fd5a995bedef0c5ff2fb67e0043dd3dcb2ae74ec2acbeb2a", ""),
    "pm_santa": ("8b012db3185778afe2e45a62cbad69db73021774fe68dda634bcc748a982eede", ""),
}

#: Voices whose training data asks for credit when the audio is published.
ATTRIBUTION = {
    "ff_siwis": "Trained on the SIWIS French speech corpus (CC BY 4.0): "
                "credit it when you publish audio made with this voice.",
}

# The model's phoneme vocabulary (tokenizer.json of the pinned revision).
_VOCAB = {
    '$': 0, ';': 1, ':': 2, ',': 3, '.': 4, '!': 5, '?': 6, '\u2014': 9, '\u2026': 10,
    '"': 11, '(': 12, ')': 13, '\u201c': 14, '\u201d': 15, ' ': 16, '\u0303': 17,
    '\u02a3': 18, '\u02a5': 19, '\u02a6': 20, '\u02a8': 21, '\u1d5d': 22, '\uab67': 23,
    'A': 24, 'I': 25, 'O': 31, 'Q': 33, 'S': 35, 'T': 36, 'W': 39, 'Y': 41,
    '\u1d4a': 42, 'a': 43, 'b': 44, 'c': 45, 'd': 46, 'e': 47, 'f': 48, 'h': 50,
    'i': 51, 'j': 52, 'k': 53, 'l': 54, 'm': 55, 'n': 56, 'o': 57, 'p': 58, 'q': 59,
    'r': 60, 's': 61, 't': 62, 'u': 63, 'v': 64, 'w': 65, 'x': 66, 'y': 67, 'z': 68,
    '\u0251': 69, '\u0250': 70, '\u0252': 71, '\xe6': 72, '\u03b2': 75, '\u0254': 76,
    '\u0255': 77, '\xe7': 78, '\u0256': 80, '\xf0': 81, '\u02a4': 82, '\u0259': 83,
    '\u025a': 85, '\u025b': 86, '\u025c': 87, '\u025f': 90, '\u0261': 92, '\u0265': 99,
    '\u0268': 101, '\u026a': 102, '\u029d': 103, '\u026f': 110, '\u0270': 111,
    '\u014b': 112, '\u0273': 113, '\u0272': 114, '\u0274': 115, '\xf8': 116,
    '\u0278': 118, '\u03b8': 119, '\u0153': 120, '\u0279': 123, '\u027e': 125,
    '\u027b': 126, '\u0281': 128, '\u027d': 129, '\u0282': 130, '\u0283': 131,
    '\u0288': 132, '\u02a7': 133, '\u028a': 135, '\u028b': 136, '\u028c': 138,
    '\u0263': 139, '\u0264': 140, '\u03c7': 142, '\u028e': 143, '\u0292': 147,
    '\u0294': 148, '\u02c8': 156, '\u02cc': 157, '\u02d0': 158, '\u02b0': 162,
    '\u02b2': 164, '\u2193': 169, '\u2192': 171, '\u2197': 172, '\u2198': 173,
    '\u1d7b': 177,
}


def is_kokoro_voice(short_name: str | None) -> bool:
    return bool(short_name) and short_name.startswith(KOKORO_PREFIX)


def _lang(key: str) -> tuple[str, str, str]:
    return LANGUAGES.get(key[:1], LANGUAGES["a"])


def display_name(key: str) -> str:
    """'af_heart' -> 'Heart'."""
    return key.partition("_")[2].replace("_", " ").title() or key


def gender_for(key: str) -> str:
    return {"f": "Female", "m": "Male"}.get(key[1:2], "")


def locale_for(key: str) -> str:
    return _lang(key)[1]


def grade_for(key: str) -> str:
    return VOICES.get(key, ("", ""))[1]


#: Grades mapped onto the Get Voices quality labels shared with Piper.
def quality_for(key: str) -> str:
    grade = grade_for(key)
    if grade.startswith("A"):
        return "high"
    if grade in ("B-", "B", "B+", "C+"):
        return "medium"
    if grade.startswith(("D", "F")):
        return "x_low"
    return "low"


_GRADE_ORDER = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "", "D+", "D", "D-", "F+", "F"]


def grade_rank(key: str) -> int:
    """Sort position: best-rated voices first (ungraded ones mid-table)."""
    grade = grade_for(key)
    return _GRADE_ORDER.index(grade) if grade in _GRADE_ORDER else len(_GRADE_ORDER)


def kokoro_available() -> bool:
    return piper_tts.piper_available()


def user_dir() -> Path:
    d = AppPaths().data_dir / "kokoro"
    (d / "voices").mkdir(parents=True, exist_ok=True)
    return d


def model_file() -> Path:
    return user_dir() / MODEL_FILE


def voice_file(key: str) -> Path:
    return user_dir() / "voices" / f"{key}.bin"


def model_installed() -> bool:
    try:
        return model_file().stat().st_size == MODEL_SIZE
    except OSError:
        return False


def installed_keys() -> set[str]:
    """Voices ready to speak: the model and the voice's style file are on disk."""
    if not model_installed():
        return set()
    found = set()
    for key in VOICES:
        try:
            if voice_file(key).stat().st_size == VOICE_SIZE:
                found.add(key)
        except OSError:
            continue
    return found


def list_kokoro_voices() -> list[dict]:
    """Installed Kokoro voices as edge-style dicts (``Source='kokoro'``)."""
    if not kokoro_available():
        return []
    voices = []
    for key in sorted(installed_keys(), key=lambda k: (grade_rank(k), k)):
        voices.append({
            "ShortName": KOKORO_PREFIX + key,
            "FriendlyName": f"{display_name(key)} (Offline HD, Kokoro) - {locale_for(key)}",
            "Locale": locale_for(key),
            "Gender": gender_for(key),
            "Source": "kokoro",
        })
    return voices


# ══════════════════════════════════════════════════════════════════════ #
#  Catalog + download                                                    #
# ══════════════════════════════════════════════════════════════════════ #

@dataclass(frozen=True)
class KokoroCatalogVoice:
    """A downloadable Kokoro voice, shaped like piper_tts.CatalogVoice."""

    key: str

    engine = "kokoro"

    @property
    def short_name(self) -> str:
        return KOKORO_PREFIX + self.key

    @property
    def locale(self) -> str:
        return locale_for(self.key)

    @property
    def language(self) -> str:
        return _lang(self.key)[2]

    @property
    def quality(self) -> str:
        return quality_for(self.key)

    @property
    def files(self) -> dict:
        return {f"voices/{self.key}.bin": {"size_bytes": VOICE_SIZE, "sha256": VOICES[self.key][0]}}

    @property
    def size_bytes(self) -> int:
        """What a download costs now: the shared model too, the first time."""
        return VOICE_SIZE + (0 if model_installed() else MODEL_SIZE)

    @property
    def display_name(self) -> str:
        return display_name(self.key)

    @property
    def gender(self) -> str:
        return gender_for(self.key)

    @property
    def note(self) -> str:
        return ATTRIBUTION.get(self.key, "")


def catalog() -> list[KokoroCatalogVoice]:
    """Every downloadable Kokoro voice (built in: no network needed)."""
    return [KokoroCatalogVoice(k) for k in sorted(VOICES, key=lambda k: (grade_rank(k), k))]


class DownloadCancelled(Exception):
    pass


def _fetch(rel: str, final: Path, size: int, sha256: str,
           report: Callable[[int], None], cancelled: Callable[[], bool] | None) -> None:
    """Download one file to ``<final>.part``, verify it, then move it into place."""
    part = final.with_name(final.name + ".part")
    digest = hashlib.sha256()
    got = 0
    try:
        with piper_tts._open(f"{BASE_URL}/{rel}?download=true", timeout=60) as resp, \
                open(part, "wb") as fh:
            while True:
                if cancelled and cancelled():
                    raise DownloadCancelled()
                block = resp.read(256 * 1024)
                if not block:
                    break
                fh.write(block)
                digest.update(block)
                got += len(block)
                report(len(block))
        if got != size:
            raise OSError(f"Download of {final.name} is incomplete ({got} of {size} bytes)")
        if digest.hexdigest() != sha256:
            raise OSError(f"Download of {final.name} is corrupted (checksum mismatch)")
        # Defender scans a fresh model for a moment after it is written; a
        # rename during the scan is a sharing violation.
        replace_with_retry(part, final, attempts=8)
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove %s", part, exc_info=True)


def download_voice(
    voice: KokoroCatalogVoice,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """
    Download *voice*, and the shared model first if it is not on disk yet.
    Each file is verified (size and SHA-256) before it is moved into place,
    so an interrupted download never leaves a broken voice.
    """
    if voice.key not in VOICES:
        raise ValueError(f"Unknown Kokoro voice: {voice.key}")
    need_model = not model_installed()
    total = VOICE_SIZE + (MODEL_SIZE if need_model else 0)
    done = 0

    def report(n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(done, total)

    if need_model:
        _fetch(f"onnx/{MODEL_FILE}", model_file(), MODEL_SIZE, MODEL_SHA256, report, cancelled)
    _fetch(f"voices/{voice.key}.bin", voice_file(voice.key), VOICE_SIZE,
           VOICES[voice.key][0], report, cancelled)
    return voice_file(voice.key)


def remove_voice(key: str) -> bool:
    """
    Delete a downloaded voice.  Removing the last one also deletes the shared
    model (~325 MB) — it is downloaded again with the next voice.
    """
    with _STYLES_LOCK:
        _STYLES.pop(key, None)
    others = installed_keys() - {key}
    removed = False
    if not others and model_file().exists():
        # Model first: if it is in use (Windows), nothing is removed.
        _evict_all()
        model_file().unlink()
        removed = True
    try:
        voice_file(key).unlink()
        removed = True
    except FileNotFoundError:
        pass
    return removed


# ══════════════════════════════════════════════════════════════════════ #
#  Text -> phonemes                                                      #
# ══════════════════════════════════════════════════════════════════════ #

# espeak-ng marks two-letter phonemes with a tie only on request, and the
# bridge Piper ships doesn't ask; these are misaki's tie-joined mappings
# (EspeakFallback / EspeakG2P) written for the untied output.  Longest first.
_EN_PAIRS = (
    ("ʔˌn̩", "ʔn"), ("ʔn̩", "ʔn"),
    ("aɪ", "I"), ("aʊ", "W"), ("dʒ", "ʤ"), ("eɪ", "A"), ("tʃ", "ʧ"), ("ɔɪ", "Y"),
    ("ʲo", "jo"), ("ʲə", "jə"), ("ʲ", ""),
    ("ɚ", "əɹ"), ("r", "ɹ"), ("x", "k"), ("ç", "k"), ("ɐ", "ə"), ("ɬ", "l"),
    ("̃", ""),
)
_OTHER_PAIRS = (
    ("aɪ", "I"), ("aʊ", "W"), ("dz", "ʣ"), ("dʒ", "ʤ"), ("eɪ", "A"),
    ("oʊ", "O"), ("əʊ", "Q"), ("ts", "ʦ"), ("tʃ", "ʧ"), ("ɔɪ", "Y"),
)
_VOWELS = "aeiouyæɑɐɒɔəɚɛɜɞɤɨɪɯʉʊʌøœɘɵIAOWYQᵻ"
#: espeak-ng's syllabic "@L" (the end of "little"): ə + l not followed by a vowel.
_SYLLABIC_L = re.compile(rf"əl(?![ˈˌː]*[{_VOWELS}])")
_LANG_FLAG = re.compile(r"\([^)]+\)")
_HUNDRED = re.compile(r"(?<=[a-zɹːɪ])(?=hˈʌndɹɪd)")
_LONE_Z = re.compile(r' z(?=[;:,.!?¡¿—…"«»“” ]|$)')


def _english(ps: str, british: bool) -> str:
    if not british:
        # "ninety" as Americans say it (not "nineteen", which keeps its ː).
        ps = re.sub(r"(?<=nˈaɪn)ti(?!ː)", "di", ps)
    ps = _HUNDRED.sub(" ", ps)
    ps = _LONE_Z.sub("z", ps)
    if british:
        ps = ps.replace("eə", "ɛː")
    else:
        ps = ps.replace("oʊ", "O")
    ps = _SYLLABIC_L.sub("ᵊl", ps)
    for old, new in _EN_PAIRS:
        ps = ps.replace(old, new)
    ps = ps.replace("e", "A")
    ps = re.sub(r"(\S)̩", r"ᵊ\1", ps).replace("̩", "")
    if british:
        ps = ps.replace("iə", "ɪə").replace("əʊ", "Q")
    else:
        ps = (ps.replace("ɜːɹ", "ɜɹ").replace("ɜː", "ɜɹ").replace("ɪə", "iə")
              .replace("ː", ""))
    ps = ps.replace("o", "ɔ")
    return ps.replace("ɾ", "T").replace("ʔ", "t")


def _other(ps: str) -> str:
    for old, new in _OTHER_PAIRS:
        ps = ps.replace(old, new)
    return ps.replace("-", "")


def to_kokoro_phonemes(ps: str, lang: str) -> str:
    """espeak-ng IPA (one clause) -> the model's phoneme alphabet."""
    ps = _LANG_FLAG.sub("", ps)
    ps = _english(ps, lang == "b") if lang in ("a", "b") else _other(ps)
    return "".join(ch for ch in ps if ch in _VOCAB)


_ESPEAK_READY = False


def _espeak_clauses(text: str, espeak_voice: str) -> list[tuple[str, str, bool]]:
    """
    espeak-ng clauses for *text*.  espeak-ng keeps global state and is not
    thread-safe, so this shares Piper's locks and its one initialisation.
    """
    global _ESPEAK_READY
    from piper import espeakbridge
    import piper.voice as piper_voice

    bridge_lock = getattr(piper_voice, "_ESPEAK_PHONEMIZER_LOCK", None) or threading.Lock()
    with piper_tts._PHONEMIZE_LOCK, bridge_lock:
        if not _ESPEAK_READY:
            if getattr(piper_voice, "_ESPEAK_PHONEMIZER", None) is None:
                from piper.phonemize_espeak import EspeakPhonemizer

                phonemizer = EspeakPhonemizer(piper_tts._espeak_data_dir())
                if hasattr(piper_voice, "_ESPEAK_PHONEMIZER"):
                    piper_voice._ESPEAK_PHONEMIZER = phonemizer
            _ESPEAK_READY = True
        espeakbridge.set_voice(espeak_voice)
        return espeakbridge.get_phonemes(text)


@dataclass
class Piece:
    """One model run: phonemes, and the pause that follows it."""

    phonemes: str
    pause: float


#: Pieces aim for the model's sweet spot (about 100-250 phonemes): much
#: longer and it starts to rush, very short and it sounds flat.
TARGET_TOKENS = 250
_MIN_TOKENS = 24
SENTENCE_PAUSE = 0.25
CLAUSE_PAUSE = 0.10
_CLAUSE_END = frozenset(",;:—")


def _split_long(ps: str, limit: int) -> list[str]:
    """Cut an over-long clause at word gaps (or hard, for one giant word)."""
    out = []
    while len(ps) > limit:
        cut = ps.rfind(" ", limit // 2, limit)
        if cut <= 0:
            out.append(ps[:limit])
            ps = ps[limit:]
        else:
            out.append(ps[:cut])
            ps = ps[cut + 1:]
    if ps.strip():
        out.append(ps)
    return out


def plan_pieces(text: str, lang: str, target: int = TARGET_TOKENS) -> list[Piece]:
    """
    Phonemize *text* and pack it into model runs: whole sentences together up
    to *target* phonemes, a long sentence split at its commas, a run-on
    clause at word gaps.  A very short sentence joins its neighbour.
    """
    espeak_voice = _lang(lang)[0]
    sentences: list[list[str]] = []
    current: list[str] = []
    for raw, term, end in _espeak_clauses(text, espeak_voice):
        clause = to_kokoro_phonemes(raw, lang).strip()
        mark = term if term in _VOCAB else ""
        if clause:
            current.append(clause + mark)
        if end and current:
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)

    pieces: list[Piece] = []
    buf = ""

    def flush(pause: float) -> None:
        nonlocal buf
        if buf.strip():
            pieces.append(Piece(buf.strip(), pause))
        buf = ""

    for clauses in sentences:
        sentence = " ".join(clauses)
        if len(sentence) <= target:
            if buf and len(buf) + 1 + len(sentence) > target and len(buf) >= _MIN_TOKENS:
                flush(SENTENCE_PAUSE)
            buf = f"{buf} {sentence}" if buf else sentence
            if len(buf) >= target:
                flush(SENTENCE_PAUSE)
            continue
        flush(SENTENCE_PAUSE)
        for clause in clauses:
            for part in _split_long(clause, target):
                if buf and len(buf) + 1 + len(part) > target:
                    flush(CLAUSE_PAUSE if buf[-1] in _CLAUSE_END else 0.0)
                buf = f"{buf} {part}" if buf else part
        flush(SENTENCE_PAUSE)
    flush(SENTENCE_PAUSE)

    # A short last piece is spoken flat on its own: join it to the one before
    # when that still fits the model.
    if len(pieces) > 1 and len(pieces[-1].phonemes) < _MIN_TOKENS:
        last = pieces.pop()
        if len(pieces[-1].phonemes) + 1 + len(last.phonemes) <= MAX_TOKENS:
            pieces[-1] = Piece(f"{pieces[-1].phonemes} {last.phonemes}", last.pause)
        else:
            pieces.append(last)
    return pieces


def tokenize(phonemes: str) -> list[int]:
    return [_VOCAB[ch] for ch in phonemes if ch in _VOCAB][:MAX_TOKENS]


# ══════════════════════════════════════════════════════════════════════ #
#  Synthesis                                                             #
# ══════════════════════════════════════════════════════════════════════ #

# One model session serves every voice.  It holds ~1 GB once it has run, so
# it is dropped when the last job or preview using Kokoro ends (reloading
# takes ~0.3 s); a synthesizer keeps its own reference meanwhile.
_SESSIONS: dict[bool, object] = {}         # on the card? -> InferenceSession
_SESSIONS_LOCK = threading.Lock()
_USERS = 0
#: Why Kokoro stopped using the graphics card this session (None: it may).
#: Kept apart from gpu_accel's flag: the 325 MB model failing on a card
#: (out of video memory beside a Piper voice) says nothing about Piper.
_GPU_FAILED: str | None = None


def _wants_gpu() -> bool:
    return _GPU_FAILED is None and piper_tts._wants_gpu()


def _card_failed(reason: str) -> None:
    global _GPU_FAILED
    if _GPU_FAILED is None:
        _GPU_FAILED = reason or "unknown error"
        logger.warning("Kokoro stopped using the graphics card (%s); it continues "
                       "on the processor", _GPU_FAILED)
_STYLES: dict[str, object] = {}
_STYLES_LOCK = threading.Lock()


def _plan_parallel_runs() -> tuple[int, int]:
    """
    (runs at once, CPU threads per run).  Two runs with the cores split
    between them are ~25% faster than one (M5: 99 -> 123 phonemes/s) but
    need ~450 MB more at peak, so only roomy machines run two.
    """
    cores = os.cpu_count() or 1
    if cores < 6 or piper_tts._total_ram_bytes() < 15 * 1024 ** 3:
        return 1, 0
    physical = cores if sys.platform == "darwin" else max(2, cores // 2)
    return 2, max(2, physical // 2)


_PARALLEL_RUNS, _THREADS_PER_RUN = _plan_parallel_runs()
_RUN_LOCK = threading.BoundedSemaphore(_PARALLEL_RUNS)


def _open_session(gpu: bool):
    import onnxruntime

    opts = onnxruntime.SessionOptions()
    if _THREADS_PER_RUN:
        opts.intra_op_num_threads = _THREADS_PER_RUN
        opts.inter_op_num_threads = 1
    # Idle worker threads otherwise busy-wait for the next run.
    opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    if gpu:
        from app.services import gpu_accel

        gpu_accel.ensure_cuda_loaded()
        providers = [("CUDAExecutionProvider", gpu_accel.cuda_provider_options()),
                     "CPUExecutionProvider"]
    else:
        providers = ["CPUExecutionProvider"]
    session = onnxruntime.InferenceSession(str(model_file()), sess_options=opts,
                                           providers=providers)
    if gpu and session.get_providers()[:1] != ["CUDAExecutionProvider"]:
        raise piper_tts.GpuUnavailable(
            "the NVIDIA CUDA provider did not start (providers: "
            + ", ".join(session.get_providers()) + ")")
    if gpu:
        # A CUDA failure in run() must raise, not quietly retry on the
        # processor (see piper_tts._open_voice).
        session.disable_fallback()
    return session


def _session(gpu: bool | None = None):
    """(session, on the card?) — on the card when GPU support is active."""
    want_gpu = _wants_gpu() if gpu is None else gpu
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(want_gpu)
        if session is not None:
            return session, want_gpu
        try:
            session = _open_session(want_gpu)
        except Exception as exc:  # noqa: BLE001
            if not want_gpu or gpu is not None:
                raise
            _card_failed(f"could not load the model on the card: {exc}")
            want_gpu = False
            session = _SESSIONS.get(False) or _open_session(False)
        else:
            if want_gpu:
                logger.info("Kokoro voices run on the graphics card")
        _SESSIONS[want_gpu] = session
        return session, want_gpu


def _evict_all() -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.clear()


def _style(key: str):
    """The voice's 510 x 256 style table, checked once per run of the app."""
    import numpy as np

    with _STYLES_LOCK:
        table = _STYLES.get(key)
        if table is None:
            data = voice_file(key).read_bytes()
            if len(data) != VOICE_SIZE or hashlib.sha256(data).hexdigest() != VOICES[key][0]:
                raise KokoroError(
                    f"The Kokoro voice “{display_name(key)}” is damaged. "
                    "Remove it and download it again from Get Voices.")
            table = np.frombuffer(data, dtype=np.float32).reshape(_STYLE_ROWS, 1, 256)
            _STYLES[key] = table
        return table


class KokoroError(RuntimeError):
    pass


class KokoroCancelled(KokoroError):
    pass


_RATE_RE = re.compile(r"\s*([+-]?\d+)\s*%\s*")


def speed_for(rate: str) -> float:
    """'+20%' -> 1.2, kept within the 0.5-2.0 the model handles well."""
    m = _RATE_RE.fullmatch(rate or "")
    return min(2.0, max(0.5, 1.0 + (int(m.group(1)) if m else 0) / 100.0))


#: The model pads each run with ~0.3 s of silence before and ~0.5 s after;
#: kept as-is, every join between runs would be a ~0.8 s gap.
_FRAME = SAMPLE_RATE // 100          # 10 ms
_SILENCE_DB = -60.0
_KEEP_BEFORE = 2                     # frames kept before the first sound
_KEEP_AFTER = 4                      # and after the last (soft endings)


def trim_silence(audio):
    """*audio* (float32) without the model's padding, keeping soft edges."""
    import numpy as np

    n = audio.size // _FRAME
    if n == 0:
        return audio
    frames = audio[: n * _FRAME].reshape(n, _FRAME)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    peak = float(rms.max())
    if peak <= 1e-6:
        return audio[:0]
    loud = np.nonzero(rms > peak * 10 ** (_SILENCE_DB / 20))[0]
    start = max(0, int(loud[0]) - _KEEP_BEFORE) * _FRAME
    end = min(audio.size, (int(loud[-1]) + 1 + _KEEP_AFTER) * _FRAME)
    return audio[start:end]


class KokoroSynthesizer:
    """Same interface as PiperSynthesizer: synthesize / cancel / close."""

    sample_rate = SAMPLE_RATE

    def __init__(self, short_name: str, *, gpu: bool | None = None) -> None:
        if not is_kokoro_voice(short_name):
            raise ValueError(f"Not a Kokoro voice: {short_name}")
        key = short_name[len(KOKORO_PREFIX):]
        self.key = key
        if key not in VOICES or key not in installed_keys():
            raise KokoroError(
                f"The Kokoro voice “{display_name(key)}” is not installed. "
                "Download it again from Get Voices, or pick another voice.")
        try:
            self._styles = _style(key)
            self._session, self._on_gpu = _session(gpu)
        except KokoroError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise KokoroError(
                f"Could not load the Kokoro voice “{display_name(key)}”: {exc}") from exc
        global _USERS
        with _SESSIONS_LOCK:
            _USERS += 1
        self._registered = True
        self._forced_device = gpu is not None
        self._swap_lock = threading.Lock()
        self._cancelled = False
        self._pool = None
        if _PARALLEL_RUNS > 1:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(_PARALLEL_RUNS, thread_name_prefix="kokoro")

    @property
    def on_gpu(self) -> bool:
        return self._on_gpu

    def synthesize(self, text: str, rate: str) -> bytes:
        import numpy as np

        if self._cancelled:
            raise KokoroCancelled("cancelled")
        speed = np.array([speed_for(rate)], dtype=np.float32)
        pieces = plan_pieces(text, self.key[:1])

        def speak(phonemes: str, depth: int = 0):
            """Float audio for *phonemes*, trimmed; None if there is none."""
            if self._cancelled:
                raise KokoroCancelled("cancelled")
            ids = tokenize(phonemes)
            if not ids:
                return None
            feed = {
                "input_ids": np.array([[0, *ids, 0]], dtype=np.int64),
                "style": self._styles[len(ids) - 1],
                "speed": speed,
            }
            audio = np.asarray(self._run(feed), dtype=np.float32).reshape(-1)
            if not np.isfinite(audio).all():
                # The model is deterministic: the same input fails again.  A
                # different cut (two halves at a word gap) usually works;
                # never let a broken run pass as a quiet gap in the book.
                cut = phonemes.find(" ", len(phonemes) // 2)
                if cut < 0:
                    cut = phonemes.rfind(" ", 0, len(phonemes) // 2)
                if depth >= 2 or cut <= 0:
                    raise KokoroError("the voice model returned invalid audio")
                logger.warning("Kokoro returned invalid audio for %d phonemes; "
                               "speaking it in two halves", len(ids))
                halves = [speak(phonemes[:cut], depth + 1), speak(phonemes[cut + 1:], depth + 1)]
                halves = [h for h in halves if h is not None and h.size]
                gap = np.zeros(int(SAMPLE_RATE * 0.05), np.float32)
                return np.concatenate([halves[0], gap, halves[1]]) if len(halves) == 2 else (
                    halves[0] if halves else None)
            return trim_silence(audio)

        def render(piece: Piece) -> bytes:
            audio = speak(piece.phonemes)
            if audio is None or not audio.size:
                return b""
            pcm = np.clip(audio * 32767.0, -32767, 32767).astype("<i2").tobytes()
            return pcm + bytes(2 * int(SAMPLE_RATE * piece.pause))

        if self._pool is not None and len(pieces) > 1:
            rendered = list(self._pool.map(render, pieces))
        else:
            rendered = [render(p) for p in pieces]
        return b"".join(rendered)

    def _run(self, feed: dict):
        session = self._session
        try:
            with _RUN_LOCK:
                if self._cancelled:
                    raise KokoroCancelled("cancelled")
                return session.run(None, feed)[0]
        except KokoroCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._on_gpu or self._forced_device:
                raise
            # Retry on the processor; only if that works was the card at fault.
            cpu, _ = _session(False)
            with _RUN_LOCK:
                if self._cancelled:
                    raise KokoroCancelled("cancelled")
                out = cpu.run(None, feed)[0]
            with self._swap_lock:
                if self._on_gpu:
                    _card_failed(str(exc) or type(exc).__name__)
                    with _SESSIONS_LOCK:
                        _SESSIONS.pop(True, None)
                    self._session, self._on_gpu = cpu, False
            return out

    def cancel(self) -> None:
        self._cancelled = True

    def close(self) -> None:
        global _USERS
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        if self._registered:
            self._registered = False
            with _SESSIONS_LOCK:
                _USERS -= 1
                if _USERS <= 0:
                    _USERS = 0
                    _SESSIONS.clear()

    def __enter__(self) -> "KokoroSynthesizer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
