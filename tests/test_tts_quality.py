from app.services import tts_quality


def test_normalize_text_for_tts_cleans_noisy_unicode():
    text = 'Hello…   world!!!\u200b “quoted” — test • bullet （sample）'

    normalized = tts_quality.normalize_text_for_tts(text)

    assert normalized == 'Hello... world!! "quoted" - test, bullet (sample)'


def test_normalize_text_for_tts_handles_bracket_heavy_narration_text():
    text = "[Quest Updated] [Return to town -> speak with the elder]"

    normalized = tts_quality.normalize_text_for_tts(text)

    assert normalized == "Quest Updated: Return to town to speak with the elder"


def test_build_text_profile_detects_hindi_text():
    profile = tts_quality.build_text_profile("यह हिंदी में एक छोटा परीक्षण है।")

    assert profile.language_code == "hi"
    assert profile.language_name == "Hindi"
    assert profile.locale_hint == "hi-IN"


def test_assess_voice_compatibility_recommends_hindi_voice():
    voices = [
        {"ShortName": "is-IS-GudrunNeural", "Locale": "is-IS", "Gender": "Female"},
        {"ShortName": "hi-IN-MadhurNeural", "Locale": "hi-IN", "Gender": "Male"},
        {"ShortName": "en-US-AvaNeural", "Locale": "en-US", "Gender": "Female"},
    ]
    profile = tts_quality.build_text_profile("यह हिंदी में एक छोटा परीक्षण है।")

    assessment = tts_quality.assess_voice_compatibility(
        profile,
        "is-IS-GudrunNeural",
        voices,
    )

    assert assessment.severity == "mismatch"
    assert "appears to be Hindi" in assessment.message
    assert "Icelandic (is-IS)" in assessment.message
    assert assessment.recommended_voice == "hi-IN-MadhurNeural"


def test_assess_voice_compatibility_flags_english_with_icelandic_voice():
    voices = [
        {"ShortName": "is-IS-GudrunNeural", "Locale": "is-IS", "Gender": "Female"},
        {"ShortName": "en-US-AvaNeural", "Locale": "en-US", "Gender": "Female"},
    ]
    profile = tts_quality.build_text_profile("This is a short English test for the voice picker.")

    assessment = tts_quality.assess_voice_compatibility(
        profile,
        "is-IS-GudrunNeural",
        voices,
    )

    assert assessment.requires_confirmation
    assert "appears to be English" in assessment.message
    assert assessment.recommended_voice == "en-US-AvaNeural"


def test_assess_voice_compatibility_handles_mixed_hindi_text():
    voices = [
        {"ShortName": "en-US-AvaNeural", "Locale": "en-US", "Gender": "Female"},
        {"ShortName": "hi-IN-MadhurNeural", "Locale": "hi-IN", "Gender": "Male"},
    ]
    profile = tts_quality.build_text_profile(
        "यह हिंदी में एक छोटा परीक्षण है, but it also mentions setup steps in English."
    )

    assessment = tts_quality.assess_voice_compatibility(
        profile,
        "en-US-AvaNeural",
        voices,
    )

    assert profile.mixed
    assert assessment.requires_confirmation
    assert "mostly Hindi" in assessment.message
    assert assessment.recommended_voice == "hi-IN-MadhurNeural"
