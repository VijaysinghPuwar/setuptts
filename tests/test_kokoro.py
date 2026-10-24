"""
Tests for the offline HD Kokoro voices: names and catalog, espeak-ng →
Kokoro phonemes, packing text into model runs, silence trimming, verified
downloads and removal, the synthesizer (with a stand-in model), GPU fallback,
and how the voices appear in the picker and in Get Voices.

Tests marked ``real_local_voices`` run the real model.  They need a folder
with the model and voices already downloaded, named by SETUPTTS_TEST_KOKORO_DIR
(the layout of the app's own ``kokoro`` folder), and skip without it.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.models.voice import Voice, persona_name  # noqa: E402
from app.services import kokoro_tts, local_tts  # noqa: E402

pytest.importorskip("pytestqt")


# ------------------------------------------------------------------ #
# Names, catalog                                                      #
# ------------------------------------------------------------------ #

def test_names_gender_locale_from_the_key():
    assert kokoro_tts.display_name("af_heart") == "Heart"
    assert kokoro_tts.display_name("bm_george") == "George"
    assert kokoro_tts.gender_for("af_heart") == "Female"
    assert kokoro_tts.gender_for("pm_santa") == "Male"
    assert kokoro_tts.locale_for("bf_emma") == "en-GB"
    assert kokoro_tts.locale_for("pf_dora") == "pt-BR"
    assert kokoro_tts.locale_for("hm_psi") == "hi-IN"


def test_catalog_lists_every_supported_voice_best_first():
    cat = kokoro_tts.catalog()
    keys = [v.key for v in cat]
    assert len(keys) == len(set(keys)) == len(kokoro_tts.VOICES) == 41
    assert keys[:4] == ["af_heart", "af_bella", "af_nicole", "bf_emma"]
    # Japanese and Chinese need pronunciation packages that aren't bundled.
    assert not any(k[0] in "jz" for k in keys)
    assert {k[0] for k in keys} == set(kokoro_tts.LANGUAGES)
    assert all(len(sha) == 64 for sha, _ in kokoro_tts.VOICES.values())


def test_quality_labels_follow_the_published_grades():
    assert kokoro_tts.quality_for("af_heart") == "high"
    assert kokoro_tts.quality_for("bf_emma") == "medium"
    assert kokoro_tts.quality_for("am_adam") == "x_low"
    assert kokoro_tts.quality_for("ef_dora") == "low"      # ungraded


def test_catalog_voice_size_includes_the_model_until_it_is_downloaded():
    v = kokoro_tts.KokoroCatalogVoice("af_heart")
    assert v.size_bytes == kokoro_tts.VOICE_SIZE + kokoro_tts.MODEL_SIZE
    _fake_model()
    assert v.size_bytes == kokoro_tts.VOICE_SIZE
    assert v.short_name == "kokoro:af_heart" and v.engine == "kokoro"


def test_attribution_note_for_cc_by_voice():
    assert "CC BY" in kokoro_tts.KokoroCatalogVoice("ff_siwis").note
    assert kokoro_tts.KokoroCatalogVoice("af_heart").note == ""


@pytest.mark.parametrize("rate, speed", [
    ("+0%", 1.0), ("+20%", 1.2), ("-25%", 0.75), ("-90%", 0.5), ("+300%", 2.0), ("junk", 1.0),
])
def test_speed_follows_rate_within_model_limits(rate, speed):
    assert kokoro_tts.speed_for(rate) == pytest.approx(speed)


def test_local_tts_routes_kokoro_voices():
    name = "kokoro:af_heart"
    assert local_tts.is_local_voice(name)
    assert local_tts.source_of(name) == local_tts.SOURCE_KOKORO
    assert local_tts.local_display_name(name) == "Heart"
    assert local_tts.local_locale(name) == "en-US"
    assert persona_name(name) == "Heart"
    v = Voice(name, "Heart", "en-US", "Female", source="kokoro")
    assert v.is_local and v.source_label == "Offline HD"
    assert Voice.from_edge_dict(v.to_edge_dict()) == v


def test_quality_module_knows_kokoro_locales():
    from app.services import tts_quality

    assert tts_quality._voice_locale("kokoro:bf_emma") == "en-GB"
    assert tts_quality._is_local_name("kokoro:af_heart")


# ------------------------------------------------------------------ #
# espeak-ng IPA → Kokoro phonemes                                     #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("ipa, lang, want", [
    ("mˈaɪ hˈaʊs", "a", "mˈI hˈWs"),                    # diphthongs
    ("ɡˈoʊ hˈoʊm", "a", "ɡˈO hˈOm"),
    ("dʒˈʌdʒ tʃˈɜːtʃ", "a", "ʤˈʌʤ ʧˈɜɹʧ"),            # affricates; ɜː → ɜɹ
    ("nˈaɪnti", "a", "nˈIndi"),                          # American "ninety"
    ("nˈaɪntiːn", "a", "nˈIntin"),                       # "nineteen" untouched
    ("lˈɪɾəl", "a", "lˈɪTᵊl"),                           # flap, syllabic l
    ("bəlˈuːn", "a", "bəlˈun"),                          # ə before a vowel: not syllabic
    ("bˈʌʔn̩", "a", "bˈʌtn"),                            # "button", as misaki maps it
    ("ɡˌəʊ hˈeə", "b", "ɡˌQ hˈɛː"),                      # British vowels
    ("nˈaɪnti", "b", "nˈInti"),                          # British keeps "t"
    ("bɔ̃ʒˈuʁ ʒə-", "f", "bɔ̃ʒˈuʁ ʒə"),                    # French: hyphen gone
    ("ɡˈʊts kˈeɪ", "i", "ɡˈʊʦ kˈA"),
    ("(en)hˈɛloʊ(es)", "e", "hˈɛlO"),                     # language-switch flags
])
def test_espeak_ipa_maps_to_kokoro_alphabet(ipa, lang, want):
    got = kokoro_tts.to_kokoro_phonemes(ipa, lang)
    assert got == want
    assert all(ch in kokoro_tts._VOCAB for ch in got)


def test_tokenize_drops_unknown_and_caps_length():
    assert kokoro_tts.tokenize("a$#") == [kokoro_tts._VOCAB["a"], kokoro_tts._VOCAB["$"]]
    assert len(kokoro_tts.tokenize("a" * 2000)) == kokoro_tts.MAX_TOKENS


# ------------------------------------------------------------------ #
# Packing text into model runs                                        #
# ------------------------------------------------------------------ #

def _clauses(monkeypatch, clauses):
    monkeypatch.setattr(kokoro_tts, "_espeak_clauses", lambda text, voice: clauses)


def test_short_sentences_share_a_run(monkeypatch):
    _clauses(monkeypatch, [("hˈaɪ", ".", True), ("jˈɛs", "!", True), ("nˈoʊ", "?", True)])
    pieces = kokoro_tts.plan_pieces("x", "a")
    assert [p.phonemes for p in pieces] == ["hˈI. jˈɛs! nˈO?"]
    assert pieces[0].pause == kokoro_tts.SENTENCE_PAUSE


def test_sentences_are_packed_up_to_the_target(monkeypatch):
    sentence = ("a" * 90, ".", True)
    _clauses(monkeypatch, [sentence] * 7)
    pieces = kokoro_tts.plan_pieces("x", "a", target=200)
    assert all(len(p.phonemes) <= 200 for p in pieces)
    assert sum(p.phonemes.count("a") for p in pieces) == 90 * 7
    assert len(pieces) == 4          # 2 + 2 + 2 + 1 sentences


def test_long_sentence_splits_at_commas_then_words(monkeypatch):
    words = " ".join(["bˈæk"] * 120)                    # ~600 phonemes, no commas
    _clauses(monkeypatch, [("ðə kˈæt", ",", False), (words, ".", True)])
    pieces = kokoro_tts.plan_pieces("x", "a", target=250)
    assert len(pieces) >= 3
    assert all(len(p.phonemes) <= 250 for p in pieces)
    assert sum(p.phonemes.count("bˈæk") for p in pieces) == 120
    assert pieces[-1].pause == kokoro_tts.SENTENCE_PAUSE
    # Cut mid-sentence at a word gap: no pause, so it still sounds like one.
    assert any(p.pause == 0.0 for p in pieces[:-1])


def test_giant_word_is_cut_hard_but_kept(monkeypatch):
    _clauses(monkeypatch, [("x" * 1300, ".", True)])
    pieces = kokoro_tts.plan_pieces("x", "a")
    assert all(len(kokoro_tts.tokenize(p.phonemes)) <= kokoro_tts.MAX_TOKENS for p in pieces)
    assert sum(p.phonemes.count("k") for p in pieces) == 1300        # x → k


def test_tiny_last_sentence_joins_the_one_before(monkeypatch):
    _clauses(monkeypatch, [("a" * 240, ".", True), ("jˈɛs", ".", True)])
    pieces = kokoro_tts.plan_pieces("x", "a", target=250)
    assert len(pieces) == 1 and pieces[0].phonemes.endswith("jˈɛs.")


def test_nothing_to_say_gives_no_pieces(monkeypatch):
    _clauses(monkeypatch, [("", "", False), ("(en)", ".", True)])
    assert kokoro_tts.plan_pieces("x", "a") == []


# ------------------------------------------------------------------ #
# Silence trimming                                                    #
# ------------------------------------------------------------------ #

def test_trim_removes_padding_but_keeps_soft_edges():
    sr = kokoro_tts.SAMPLE_RATE
    tone = (0.5 * np.sin(np.arange(sr) / 5)).astype(np.float32)
    audio = np.concatenate([np.zeros(int(sr * 0.3), np.float32), tone,
                            np.zeros(int(sr * 0.5), np.float32)])
    out = kokoro_tts.trim_silence(audio)
    assert sr <= out.size <= sr + 0.08 * sr
    assert np.abs(out[: out.size // 2]).max() > 0.4


def test_trim_of_silence_is_empty():
    assert kokoro_tts.trim_silence(np.zeros(48_000, np.float32)).size == 0
    assert kokoro_tts.trim_silence(np.zeros(10, np.float32)).size == 10


# ------------------------------------------------------------------ #
# Downloads                                                           #
# ------------------------------------------------------------------ #

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _serve(monkeypatch, files: dict[str, bytes], seen: list | None = None):
    """Serve fake model/voice bodies and make their checksums match."""
    def fake_open(url, timeout=30):
        rel = url.split(kokoro_tts.REVISION + "/", 1)[1].split("?", 1)[0]
        if seen is not None:
            seen.append(rel)
        return _Resp(files[rel])

    monkeypatch.setattr(kokoro_tts.piper_tts, "_open", fake_open)
    model = files.get(f"onnx/{kokoro_tts.MODEL_FILE}")
    if model is not None:
        monkeypatch.setattr(kokoro_tts, "MODEL_SIZE", len(model))
        monkeypatch.setattr(kokoro_tts, "MODEL_SHA256", hashlib.sha256(model).hexdigest())
    voices = dict(kokoro_tts.VOICES)
    for rel, body in files.items():
        if rel.startswith("voices/"):
            key = rel[len("voices/"):-len(".bin")]
            voices[key] = (hashlib.sha256(body).hexdigest(), voices[key][1])
    monkeypatch.setattr(kokoro_tts, "VOICES", voices)


def _voice_body(seed=1) -> bytes:
    return np.random.default_rng(seed).standard_normal(
        kokoro_tts._STYLE_ROWS * 256).astype(np.float32).tobytes()


def _fake_model(body: bytes = b"MODEL" * 100) -> None:
    kokoro_tts.model_file().write_bytes(body)
    kokoro_tts.MODEL_SIZE = len(body)    # undone by the tests that rely on it


@pytest.fixture(autouse=True)
def _no_piper_voices(request, monkeypatch, tmp_path):
    """Get Voices tests see only the Kokoro voices they set up."""
    if request.node.get_closest_marker("real_local_voices"):
        return
    from app.services import piper_tts

    (tmp_path / "piper").mkdir()
    monkeypatch.setattr(piper_tts, "user_dir", lambda: tmp_path / "piper")
    monkeypatch.setattr(piper_tts, "bundled_dir", lambda: tmp_path / "no-bundled")


@pytest.fixture(autouse=True)
def _restore_model_size(monkeypatch):
    monkeypatch.setattr(kokoro_tts, "MODEL_SIZE", kokoro_tts.MODEL_SIZE)
    monkeypatch.setattr(kokoro_tts, "_SESSIONS", {})
    monkeypatch.setattr(kokoro_tts, "_STYLES", {})
    monkeypatch.setattr(kokoro_tts, "_USERS", 0)
    monkeypatch.setattr(kokoro_tts, "_GPU_FAILED", None)


def test_first_download_fetches_model_then_voice(monkeypatch):
    model, voice = b"M" * 4000, _voice_body()
    seen, progress = [], []
    _serve(monkeypatch, {f"onnx/{kokoro_tts.MODEL_FILE}": model,
                         "voices/af_heart.bin": voice}, seen)
    kokoro_tts.download_voice(kokoro_tts.KokoroCatalogVoice("af_heart"),
                              progress=lambda d, t: progress.append((d, t)))
    assert seen == [f"onnx/{kokoro_tts.MODEL_FILE}", "voices/af_heart.bin"]
    assert kokoro_tts.installed_keys() == {"af_heart"}
    assert progress[-1] == (len(model) + len(voice),) * 2
    assert not list(kokoro_tts.user_dir().rglob("*.part"))


def test_second_voice_reuses_the_model(monkeypatch):
    model = b"M" * 4000
    _serve(monkeypatch, {f"onnx/{kokoro_tts.MODEL_FILE}": model,
                         "voices/af_heart.bin": _voice_body(1),
                         "voices/bf_emma.bin": _voice_body(2)})
    kokoro_tts.download_voice(kokoro_tts.KokoroCatalogVoice("af_heart"))
    seen = []
    _serve(monkeypatch, {"voices/bf_emma.bin": _voice_body(2)}, seen)
    kokoro_tts.download_voice(kokoro_tts.KokoroCatalogVoice("bf_emma"))
    assert seen == ["voices/bf_emma.bin"]
    assert kokoro_tts.installed_keys() == {"af_heart", "bf_emma"}


def test_corrupt_voice_download_installs_nothing(monkeypatch):
    model = b"M" * 4000
    _serve(monkeypatch, {f"onnx/{kokoro_tts.MODEL_FILE}": model,
                         "voices/af_heart.bin": _voice_body()})
    good = kokoro_tts.VOICES["af_heart"]
    monkeypatch.setitem(kokoro_tts.VOICES, "af_heart", ("0" * 64, good[1]))
    with pytest.raises(OSError, match="corrupted"):
        kokoro_tts.download_voice(kokoro_tts.KokoroCatalogVoice("af_heart"))
    assert kokoro_tts.installed_keys() == set()
    assert not kokoro_tts.voice_file("af_heart").exists()
    assert kokoro_tts.model_installed()           # the verified model is kept


def test_truncated_model_download_installs_nothing(monkeypatch):
    _serve(monkeypatch, {f"onnx/{kokoro_tts.MODEL_FILE}": b"M" * 100,
                         "voices/af_heart.bin": _voice_body()})
    monkeypatch.setattr(kokoro_tts, "MODEL_SIZE", 5000)
    with pytest.raises(OSError, match="incomplete"):
        kokoro_tts.download_voice(kokoro_tts.KokoroCatalogVoice("af_heart"))
    assert not kokoro_tts.model_file().exists()
    assert not list(kokoro_tts.user_dir().rglob("*.part"))


def test_cancelled_download_installs_nothing(monkeypatch):
    _serve(monkeypatch, {f"onnx/{kokoro_tts.MODEL_FILE}": b"M" * 4000,
                         "voices/af_heart.bin": _voice_body()})
    with pytest.raises(kokoro_tts.DownloadCancelled):
        kokoro_tts.download_voice(kokoro_tts.KokoroCatalogVoice("af_heart"),
                                  cancelled=lambda: True)
    assert kokoro_tts.installed_keys() == set()
    assert not list(kokoro_tts.user_dir().rglob("*.part"))


def test_voice_without_model_is_not_installed():
    kokoro_tts.voice_file("af_heart").write_bytes(_voice_body())
    assert kokoro_tts.installed_keys() == set()
    assert kokoro_tts.list_kokoro_voices() == []


def test_removing_the_last_voice_removes_the_model():
    _fake_model()
    for key in ("af_heart", "bf_emma"):
        kokoro_tts.voice_file(key).write_bytes(_voice_body())
    kokoro_tts._SESSIONS[False] = object()
    assert kokoro_tts.remove_voice("bf_emma")
    assert kokoro_tts.model_file().exists() and kokoro_tts._SESSIONS
    assert kokoro_tts.remove_voice("af_heart")
    assert not kokoro_tts.model_file().exists()
    assert not kokoro_tts._SESSIONS                # evicted before deleting
    assert kokoro_tts.installed_keys() == set()


def test_model_in_use_keeps_the_last_voice(monkeypatch):
    _fake_model()
    kokoro_tts.voice_file("af_heart").write_bytes(_voice_body())
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self.name == kokoro_tts.MODEL_FILE:
            raise PermissionError("in use")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(PermissionError):
        kokoro_tts.remove_voice("af_heart")
    assert kokoro_tts.voice_file("af_heart").exists()


def test_installed_voices_listed_best_first():
    _fake_model()
    for key in ("am_adam", "af_heart", "bf_emma"):
        kokoro_tts.voice_file(key).write_bytes(b"x" * kokoro_tts.VOICE_SIZE)
    voices = kokoro_tts.list_kokoro_voices()
    assert [v["ShortName"] for v in voices] == ["kokoro:af_heart", "kokoro:bf_emma",
                                                "kokoro:am_adam"]
    assert voices[0]["Source"] == "kokoro" and voices[0]["Gender"] == "Female"


# ------------------------------------------------------------------ #
# Synthesizer (stand-in model)                                        #
# ------------------------------------------------------------------ #

class FakeSession:
    """Returns a tone as long as the input (with the model's padding)."""

    def __init__(self, *, nan=False, fail=False):
        self.nan, self.fail = nan, fail
        self.feeds = []

    def run(self, _outputs, feed):
        self.feeds.append(feed)
        if self.fail:
            raise RuntimeError("CUDA failure")
        n = feed["input_ids"].shape[1] * 600
        pad = np.zeros(7200, np.float32)
        tone = (0.4 * np.sin(np.arange(n) / 4)).astype(np.float32)
        out = np.concatenate([pad, tone, pad])
        if self.nan:
            out[:] = np.nan
        return [out.reshape(1, -1)]

    def get_providers(self):
        return ["CPUExecutionProvider"]


def _install(monkeypatch, key="af_heart", session=None):
    _fake_model()
    body = _voice_body()
    kokoro_tts.voice_file(key).write_bytes(body)
    monkeypatch.setitem(kokoro_tts.VOICES, key,
                        (hashlib.sha256(body).hexdigest(), kokoro_tts.VOICES[key][1]))
    session = session or FakeSession()
    monkeypatch.setattr(kokoro_tts, "_open_session", lambda gpu: session)
    monkeypatch.setattr(kokoro_tts.piper_tts, "_wants_gpu", lambda: False)
    return session, np.frombuffer(body, np.float32).reshape(kokoro_tts._STYLE_ROWS, 1, 256)


def test_synthesizer_feeds_the_model_and_trims(monkeypatch):
    session, styles = _install(monkeypatch)
    monkeypatch.setattr(kokoro_tts, "_espeak_clauses",
                        lambda text, voice: [("hˈɛloʊ wˈɜːld", ".", True)])
    with kokoro_tts.KokoroSynthesizer("kokoro:af_heart") as synth:
        pcm = synth.synthesize("Hello world.", "+20%")
    feed = session.feeds[0]
    ids = feed["input_ids"][0].tolist()
    assert ids[0] == ids[-1] == 0 and len(ids) == len("hˈɛlO wˈɜɹld.") + 2
    assert np.array_equal(feed["style"], styles[len(ids) - 3])   # row = tokens - 1
    assert feed["speed"].dtype == np.float32 and feed["speed"][0] == pytest.approx(1.2)
    seconds = len(pcm) / 2 / kokoro_tts.SAMPLE_RATE
    voiced = (len(ids) * 600) / kokoro_tts.SAMPLE_RATE
    # Padding trimmed (0.6 s of it), the sentence pause added.
    assert voiced < seconds < voiced + kokoro_tts.SENTENCE_PAUSE + 0.1


def test_invalid_model_output_is_an_error_not_silence(monkeypatch):
    _install(monkeypatch, session=FakeSession(nan=True))
    monkeypatch.setattr(kokoro_tts, "_espeak_clauses", lambda t, v: [("hˈɛloʊ", ".", True)])
    synth = kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    with pytest.raises(kokoro_tts.KokoroError, match="invalid audio"):
        synth.synthesize("Hello.", "+0%")
    synth.close()


class _NanForLongInput(FakeSession):
    """Fails like the fp16 build did: invalid audio for one exact input."""

    def run(self, outputs, feed):
        out = super().run(outputs, feed)
        if feed["input_ids"].shape[1] > 40:
            out[0][:] = np.nan
        return out


def test_invalid_audio_is_recovered_by_speaking_two_halves(monkeypatch):
    session, _ = _install(monkeypatch, session=_NanForLongInput())
    words = " ".join(["hˈɛloʊ"] * 9)                 # 9 x 6 = 62 phonemes
    monkeypatch.setattr(kokoro_tts, "_espeak_clauses", lambda t, v: [(words, ".", True)])
    synth = kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    pcm = synth.synthesize("x", "+0%")
    synth.close()
    lengths = [f["input_ids"].shape[1] for f in session.feeds]
    assert lengths[0] > 40 and all(n <= 40 for n in lengths[1:]) and len(lengths) == 3
    # Every phoneme was spoken: both halves' audio is in the result.
    voiced = sum(n * 600 for n in lengths[1:]) / kokoro_tts.SAMPLE_RATE
    assert len(pcm) / 2 / kokoro_tts.SAMPLE_RATE > voiced


def test_cancel_stops_before_the_next_run(monkeypatch):
    session, _ = _install(monkeypatch)
    monkeypatch.setattr(kokoro_tts, "_espeak_clauses", lambda t, v: [("a" * 200, ".", True)] * 6)
    synth = kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    synth.cancel()
    with pytest.raises(kokoro_tts.KokoroCancelled):
        synth.synthesize("x", "+0%")
    assert session.feeds == []
    synth.close()
    assert issubclass(kokoro_tts.KokoroCancelled, local_tts.CANCELLED_ERRORS)


def test_missing_voice_is_a_plain_error():
    with pytest.raises(kokoro_tts.KokoroError, match="not installed"):
        kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    with pytest.raises(ValueError):
        kokoro_tts.KokoroSynthesizer("piper:en_US-lessac-medium")


def test_damaged_voice_file_is_reported(monkeypatch):
    _install(monkeypatch)
    data = bytearray(kokoro_tts.voice_file("af_heart").read_bytes())
    data[100] ^= 0xFF
    kokoro_tts.voice_file("af_heart").write_bytes(bytes(data))
    with pytest.raises(kokoro_tts.KokoroError, match="damaged"):
        kokoro_tts.KokoroSynthesizer("kokoro:af_heart")


def test_one_model_session_serves_every_voice(monkeypatch):
    opened = []
    _install(monkeypatch, "af_heart")
    _install(monkeypatch, "bf_emma")
    monkeypatch.setattr(kokoro_tts, "_open_session",
                        lambda gpu: opened.append(gpu) or FakeSession())
    a = kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    b = kokoro_tts.KokoroSynthesizer("kokoro:bf_emma")
    assert opened == [False] and a._session is b._session
    a.close()
    assert kokoro_tts._SESSIONS                    # still in use by b
    b.close()
    b.close()                                      # closing twice is harmless
    assert not kokoro_tts._SESSIONS and kokoro_tts._USERS == 0
    # The next voice loads the model again.
    kokoro_tts.KokoroSynthesizer("kokoro:af_heart").close()
    assert opened == [False, False]


def test_gpu_failure_mid_job_finishes_on_the_processor(monkeypatch):
    from app.services import gpu_accel

    card, cpu = FakeSession(fail=True), FakeSession()
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: pytest.fail("Piper's card flag"))
    _install(monkeypatch)
    monkeypatch.setattr(kokoro_tts, "_open_session", lambda gpu: card if gpu else cpu)
    monkeypatch.setattr(kokoro_tts.piper_tts, "_wants_gpu", lambda: True)
    monkeypatch.setattr(kokoro_tts, "_espeak_clauses", lambda t, v: [("hˈɛloʊ", ".", True)])
    synth = kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    assert synth.on_gpu
    assert synth.synthesize("Hello.", "+0%")
    assert kokoro_tts._GPU_FAILED and not synth.on_gpu and cpu.feeds
    synth.synthesize("Hello.", "+0%")
    assert len(card.feeds) == 1                    # the card is not tried again
    synth.close()
    # Later Kokoro jobs start on the processor; Piper keeps the card.
    again = kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    assert not again.on_gpu
    again.close()


def test_gpu_that_will_not_load_falls_back_at_start(monkeypatch):
    from app.services import gpu_accel

    _install(monkeypatch)
    cpu = FakeSession()

    def open_session(gpu):
        if gpu:
            raise RuntimeError("no CUDA")
        return cpu

    monkeypatch.setattr(kokoro_tts, "_open_session", open_session)
    monkeypatch.setattr(kokoro_tts.piper_tts, "_wants_gpu", lambda: True)
    monkeypatch.setattr(gpu_accel, "mark_failed", lambda r: pytest.fail("Piper's card flag"))
    synth = kokoro_tts.KokoroSynthesizer("kokoro:af_heart")
    assert not synth.on_gpu and synth._session is cpu and "no CUDA" in kokoro_tts._GPU_FAILED
    synth.close()


def test_local_worker_speaks_a_kokoro_job(qtbot, monkeypatch, tmp_path):
    from app.utils.mp3_duration import mp3_duration_seconds
    from app.workers.local_tts_worker import LocalTTSWorker

    _install(monkeypatch)
    monkeypatch.setattr(kokoro_tts, "_espeak_clauses",
                        lambda text, voice: [("hˈɛloʊ ðˈɛɹ", ".", True)] * max(1, text.count(".")))
    out = tmp_path / "book.mp3"
    worker = LocalTTSWorker("Hello there. " * 400, "kokoro:af_heart", "+0%", "+0%", str(out))
    done = []
    worker.completed.connect(lambda p, s: done.append(p))
    with qtbot.waitSignal(worker.completed, timeout=60_000):
        worker.start()
    worker.wait()
    assert done and Path(done[0]).exists()
    assert mp3_duration_seconds(out) > 60


# ------------------------------------------------------------------ #
# Picker and Get Voices                                               #
# ------------------------------------------------------------------ #

@pytest.fixture
def window(qapp, tmp_path, monkeypatch, qtbot):
    from app.config.settings import AppSettings
    from app.ui.main_window import MainWindow
    from app.utils.paths import AppPaths
    from app.workers import voice_loader

    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    paths = AppPaths()
    settings = AppSettings(paths)
    settings.language_filter = ""
    win = MainWindow(settings=settings, paths=paths)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.ensure_workers_stopped()


VOICES = [
    Voice("en-US-AvaNeural", "Ava", "en-US", "Female"),
    Voice("kokoro:am_adam", "Adam", "en-US", "Male", source="kokoro"),
    Voice("kokoro:af_heart", "Heart", "en-US", "Female", source="kokoro"),
    Voice("piper:en_US-lessac-medium", "Lessac", "en-US", "Female", source="piper"),
    Voice("system:Cortana", "Cortana", "en-US", "Female", source="system"),
]


def _rows(panel):
    combo = panel._voice_combo
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_picker_lists_kokoro_first_among_offline_voices(window):
    panel = window._output_panel
    panel._on_voices_loaded(list(VOICES))
    rows = _rows(panel)
    headers = [t for t, d in rows if d is None]
    assert headers == ["── Microsoft Online ──", "── Offline HD (Kokoro) ──",
                       "── Offline Neural ──", "── Built into this computer ──"]
    names = [d for _, d in rows if d]
    # Best-rated first within the Kokoro group.
    assert names.index("kokoro:af_heart") < names.index("kokoro:am_adam")
    assert any("Heart" in t and "Offline HD" in t for t, _ in rows)


def test_offline_tab_shows_kokoro_and_piper(window, qtbot):
    panel = window._output_panel
    panel._on_voices_loaded(list(VOICES))
    panel._source_buttons["piper"].click()
    qtbot.waitUntil(lambda: all(d is None or not d.startswith(("en-", "system:"))
                                for _, d in _rows(panel)), timeout=2000)
    assert {d for _, d in _rows(panel) if d} == {
        "kokoro:af_heart", "kokoro:am_adam", "piper:en_US-lessac-medium"}
    assert "3 voice" in panel._source_buttons["piper"].toolTip()


def test_offline_tab_with_only_kokoro_is_not_empty(window):
    panel = window._output_panel
    panel._on_voices_loaded([v for v in VOICES if v.source != "piper"])
    panel._source_buttons["piper"].click()
    panel._apply_filters()
    assert panel._has_visible_voice()
    assert panel.get_selected_voice().startswith("kokoro:")


def test_saved_kokoro_voice_is_restored_with_its_note(window):
    panel = window._output_panel
    panel._settings.voice = "kokoro:af_heart"
    panel._on_voices_loaded(list(VOICES))
    assert panel.get_selected_voice() == "kokoro:af_heart"
    assert "Kokoro" in panel._voice_combo.toolTip()


def test_job_label_for_kokoro(window):
    from app.ui.panels.output_panel import _job_voice_display

    assert _job_voice_display("kokoro:af_heart") == "Heart · Offline HD"


def test_get_voices_lists_kokoro_first_and_downloads_it(qapp, qtbot, monkeypatch):
    from app.ui.dialogs import voice_manager_dialog as vmd

    monkeypatch.setattr(vmd._CatalogLoader, "start", lambda self: None)
    dialog = vmd.VoiceManagerDialog()
    qtbot.addWidget(dialog)
    dialog._on_catalog([])
    table = dialog._table
    assert table.rowCount() == len(kokoro_tts.VOICES)
    assert table.item(0, vmd._COL_VOICE).text() == "Heart · Female"
    assert table.item(0, vmd._COL_QUALITY).text() == "HD · Best"
    assert table.item(0, vmd._COL_SIZE).text() == "326 MB"         # with the model

    model, voice = b"M" * 4000, _voice_body()
    _serve(monkeypatch, {f"onnx/{kokoro_tts.MODEL_FILE}": model,
                         "voices/af_heart.bin": voice})
    table.cellWidget(0, vmd._COL_ACTION).click()
    qtbot.waitUntil(lambda: dialog.changed, timeout=10_000)
    assert dialog.last_installed == "kokoro:af_heart"
    assert table.cellWidget(0, vmd._COL_ACTION).text() == "Remove"
    # The model is shared now: other voices cost under 1 MB.
    assert table.item(1, vmd._COL_SIZE).text() == "522 KB"

    asked = []
    monkeypatch.setattr(vmd.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or vmd.QMessageBox.StandardButton.Yes)
    table.cellWidget(0, vmd._COL_ACTION).click()
    assert "last Kokoro voice" in asked[0]
    assert not kokoro_tts.model_installed() and dialog.last_installed is None


def test_get_voices_engine_filter(qapp, qtbot, monkeypatch):
    from app.ui.dialogs import voice_manager_dialog as vmd
    from app.services import piper_tts

    monkeypatch.setattr(vmd._CatalogLoader, "start", lambda self: None)
    dialog = vmd.VoiceManagerDialog()
    qtbot.addWidget(dialog)
    piper = piper_tts.CatalogVoice("en_US-fake-low", "en-US", "English (US)", "low", 10,
                                   {"a.onnx": {}, "a.onnx.json": {}})
    dialog._on_catalog([piper])
    assert dialog._table.rowCount() == len(kokoro_tts.VOICES) + 1
    dialog._engine_combo.setCurrentIndex(dialog._engine_combo.findData("piper"))
    assert dialog._table.rowCount() == 1
    dialog._engine_combo.setCurrentIndex(dialog._engine_combo.findData("kokoro"))
    assert dialog._table.rowCount() == len(kokoro_tts.VOICES)
    dialog._search.setText("siwis")
    qtbot.waitUntil(lambda: dialog._table.rowCount() == 1, timeout=2000)
    assert "CC BY" in dialog._table.item(0, vmd._COL_VOICE).toolTip()


def test_get_voices_keeps_kokoro_when_piper_catalog_fails(qapp, qtbot, monkeypatch):
    from app.ui.dialogs import voice_manager_dialog as vmd

    monkeypatch.setattr(vmd._CatalogLoader, "start", lambda self: None)
    dialog = vmd.VoiceManagerDialog()
    qtbot.addWidget(dialog)
    dialog._on_catalog_failed("couldn't reach the download server")
    assert dialog._table.rowCount() == len(kokoro_tts.VOICES)
    assert "Kokoro HD voices are listed" in dialog._status.text()


# ------------------------------------------------------------------ #
# The real model                                                      #
# ------------------------------------------------------------------ #

@pytest.fixture
def real_kokoro(monkeypatch):
    root = os.environ.get("SETUPTTS_TEST_KOKORO_DIR")
    if not root or not (Path(root) / kokoro_tts.MODEL_FILE).exists():
        pytest.skip("set SETUPTTS_TEST_KOKORO_DIR to a folder with the Kokoro model")
    monkeypatch.setattr(kokoro_tts, "user_dir", lambda: Path(root))
    if "af_heart" not in kokoro_tts.installed_keys():
        pytest.skip("af_heart is not downloaded there")
    return Path(root)


@pytest.mark.real_local_voices
def test_real_kokoro_speaks_and_follows_speed(real_kokoro):
    with kokoro_tts.KokoroSynthesizer("kokoro:af_heart") as synth:
        text = "Testing one two three. The second sentence is a little longer!"
        pcm = synth.synthesize(text, "+0%")
        assert len(pcm) / 2 / synth.sample_rate > 2.0
        fast = synth.synthesize(text, "+100%")
        assert len(fast) < len(pcm) * 0.7


@pytest.mark.real_local_voices
def test_real_kokoro_never_returns_invalid_audio(real_kokoro):
    """The fp16 build gave all-NaN audio for 20 of 88 of these passages."""
    sentences = [
        "The old man looked at the sea for a long time, and then he turned back toward the village.",
        "Nobody had ever told him that the tide could move so quickly.",
        "He walked slowly, counting the boats that had returned.",
        "It was the best of times, it was the worst of times, it was the age of wisdom.",
        "Yes.",
    ]
    voices = [k for k in ("af_heart", "am_michael", "bf_emma") if k in kokoro_tts.installed_keys()]
    for key in voices:
        with kokoro_tts.KokoroSynthesizer(kokoro_tts.KOKORO_PREFIX + key) as synth:
            for i in range(len(sentences)):
                assert synth.synthesize(" ".join(sentences[i:] + sentences[:i]), "+0%")


@pytest.mark.real_local_voices
def test_real_kokoro_and_piper_share_espeak_safely(real_kokoro):
    """espeak-ng is global: two engines in two languages at once must not mix."""
    from app.services import piper_tts

    if piper_tts.model_path(piper_tts.BUNDLED_VOICE) is None:
        pytest.skip("bundled Piper voice not present")
    errors, lengths = [], []

    def kokoro_job(key, text):
        try:
            with kokoro_tts.KokoroSynthesizer(kokoro_tts.KOKORO_PREFIX + key) as s:
                for _ in range(3):
                    lengths.append(len(s.synthesize(text, "+0%")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def piper_job():
        try:
            with piper_tts.PiperSynthesizer(piper_tts.PIPER_PREFIX + piper_tts.BUNDLED_VOICE) as s:
                for _ in range(3):
                    lengths.append(len(s.synthesize("Piper keeps speaking English.", "+0%")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    jobs = [threading.Thread(target=kokoro_job, args=("af_heart", "Kokoro speaks English here."))]
    if "ff_siwis" in kokoro_tts.installed_keys():
        jobs.append(threading.Thread(target=kokoro_job, args=("ff_siwis", "Bonjour tout le monde.")))
    jobs.append(threading.Thread(target=piper_job))
    for t in jobs:
        t.start()
    for t in jobs:
        t.join(300)
    assert not errors and len(lengths) == 3 * len(jobs) and all(lengths)
    # Same text gives the same phonemes whatever ran in between.
    first = kokoro_tts.plan_pieces("Kokoro speaks English here.", "a")
    piper_tts.PiperSynthesizer(piper_tts.PIPER_PREFIX + piper_tts.BUNDLED_VOICE).synthesize(
        "Bonjour.", "+0%")
    assert kokoro_tts.plan_pieces("Kokoro speaks English here.", "a") == first


def test_remove_is_disabled_while_a_download_runs(qapp, qtbot, monkeypatch):
    """Removing the last Kokoro voice mid-download would delete the model."""
    from app.ui.dialogs import voice_manager_dialog as vmd

    _fake_model()
    kokoro_tts.voice_file("af_heart").write_bytes(b"x" * kokoro_tts.VOICE_SIZE)
    monkeypatch.setattr(vmd._CatalogLoader, "start", lambda self: None)
    gate = threading.Event()

    def slow_download(voice, progress=None, cancelled=None):
        gate.wait(10)

    monkeypatch.setattr(kokoro_tts, "download_voice", slow_download)
    dialog = vmd.VoiceManagerDialog()
    qtbot.addWidget(dialog)
    dialog._on_catalog([])
    remove = dialog._table.cellWidget(0, vmd._COL_ACTION)
    assert remove.text() == "Remove" and remove.isEnabled()
    dialog._table.cellWidget(1, vmd._COL_ACTION).click()          # download Bella
    assert not dialog._table.cellWidget(0, vmd._COL_ACTION).isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: not dialog._downloader.isRunning(), timeout=10_000)
    qtbot.waitUntil(lambda: dialog._table.cellWidget(0, vmd._COL_ACTION).isEnabled(),
                    timeout=5_000)


def test_failed_remove_explains_instead_of_crashing(qapp, qtbot, monkeypatch):
    from app.ui.dialogs import voice_manager_dialog as vmd

    _fake_model()
    kokoro_tts.voice_file("af_heart").write_bytes(b"x" * kokoro_tts.VOICE_SIZE)
    monkeypatch.setattr(vmd._CatalogLoader, "start", lambda self: None)
    dialog = vmd.VoiceManagerDialog()
    qtbot.addWidget(dialog)
    dialog._on_catalog([])

    def busy(key):
        raise PermissionError("in use")

    monkeypatch.setattr(kokoro_tts, "remove_voice", busy)
    monkeypatch.setattr(vmd.QMessageBox, "question",
                        lambda *a, **k: vmd.QMessageBox.StandardButton.Yes)
    warned = []
    monkeypatch.setattr(vmd.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog._remove("kokoro:af_heart")
    assert warned and "in use" in warned[0]


def test_language_list_has_one_entry_per_locale(qapp, qtbot, monkeypatch):
    from app.services import piper_tts
    from app.ui.dialogs import voice_manager_dialog as vmd

    monkeypatch.setattr(vmd._CatalogLoader, "start", lambda self: None)
    dialog = vmd.VoiceManagerDialog()
    qtbot.addWidget(dialog)
    gb = piper_tts.CatalogVoice("en_GB-alan-low", "en-GB", "English (Great Britain)", "low",
                                10, {"a.onnx": {}, "a.onnx.json": {}})
    dialog._on_catalog([gb])
    locales = [dialog._lang.itemData(i) for i in range(dialog._lang.count())]
    assert len(locales) == len(set(locales))
    assert dialog._lang.findText("English (Great Britain)") >= 0


def test_recommended_kokoro_voice_stays_on_the_offline_tab(window):
    panel = window._output_panel
    panel._on_voices_loaded(list(VOICES))
    panel._source_buttons["piper"].click()
    panel._apply_filters()
    assert panel._select_voice_by_short_name("kokoro:am_adam")
    assert panel._current_source_filter() == "piper"
    assert panel.get_selected_voice() == "kokoro:am_adam"
