"""Phonemes a Piper voice has no id for are dropped quietly, with identical ids."""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

from app.services import piper_tts

SYLLABIC = "̩"   # espeak-ng's syllabic mark, as in "n̩"

ID_MAP = {"^": [1], "_": [0], "$": [2], "b": [10], "ʌ": [11], "t": [12], "n": [13], " ": [3]}


def _voice(id_map=ID_MAP):
    return SimpleNamespace(config=SimpleNamespace(phoneme_id_map=id_map))


def _piper_ids(phonemes, id_map=ID_MAP):
    from piper.phoneme_ids import phonemes_to_ids
    return phonemes_to_ids(phonemes, id_map)


BUTTON = ["b", "ʌ", "t", SYLLABIC, "n", " ", "b", "ʌ", "t", "n", SYLLABIC]


def test_unknown_phoneme_dropped_with_identical_ids():
    voice = _voice()
    kept = piper_tts._known_phonemes(voice, BUTTON)
    assert SYLLABIC not in kept
    assert _piper_ids(kept) == _piper_ids(BUTTON)


def test_no_piper_warning_and_one_info_line(caplog):
    voice = _voice()
    with caplog.at_level(logging.INFO):
        for _ in range(50):
            _piper_ids(piper_tts._known_phonemes(voice, BUTTON))
    assert not [r for r in caplog.records if "Missing phoneme" in r.getMessage()]
    infos = [r for r in caplog.records if "no sound for phoneme" in r.getMessage()]
    assert len(infos) == 1 and "U+0329" in infos[0].getMessage()


def test_all_known_returns_same_list_object():
    voice = _voice()
    phonemes = ["b", "ʌ", "t", "n"]
    assert piper_tts._known_phonemes(voice, phonemes) is phonemes


@pytest.mark.parametrize("config", [SimpleNamespace(), SimpleNamespace(phoneme_id_map=None),
                                    SimpleNamespace(phoneme_id_map={})])
def test_voice_without_id_map_is_left_alone(config):
    voice = SimpleNamespace(config=config)
    assert piper_tts._known_phonemes(voice, BUTTON) is BUTTON


def test_concurrent_first_use_is_safe():
    voice = _voice()
    errors = []
    start = threading.Barrier(8)

    def work():
        try:
            start.wait()
            for _ in range(200):
                assert SYLLABIC not in piper_tts._known_phonemes(voice, BUTTON)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_synthesizer_passes_filtered_phonemes(monkeypatch, tmp_path):
    seen = []

    class Voice:
        config = SimpleNamespace(sample_rate=22050, length_scale=1.0, phoneme_id_map=ID_MAP)

        def phonemize(self, text):
            return [list(BUTTON)]

        def phonemes_to_ids(self, phonemes):
            seen.append(list(phonemes))
            return _piper_ids(phonemes)

        def phoneme_ids_to_audio(self, ids, cfg):
            import numpy as np
            return np.ones(100, dtype="float32")

    model = tmp_path / "en_US-test-medium.onnx"
    model.write_bytes(b"")
    monkeypatch.setattr(piper_tts, "model_path", lambda key: model)
    monkeypatch.setattr(piper_tts, "_open_voice", lambda path, gpu=False: Voice())
    piper_tts._LOADED.clear()
    try:
        with piper_tts.PiperSynthesizer("piper:en_US-test-medium") as synth:
            assert synth.synthesize("button button", "+0%")
    finally:
        piper_tts._LOADED.clear()
    assert seen and all(SYLLABIC not in s for s in seen)
