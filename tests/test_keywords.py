"""Marcado por palabras clave (paso 5)."""
from radar.config import load_keywords
from radar.keywords import compile_keywords, match_review

KW = compile_keywords(load_keywords())


def test_case_insensitive_whole_word():
    assert match_review("It is BROKEN since the update", KW) == {"broken": ["broken"]}
    assert "broken" not in match_review("We debugged it", KW)          # "bug" dentro de "debugged"
    assert "performance" not in match_review("slowly but surely", KW)  # "slow" dentro de "slowly"


def test_typographic_apostrophe():
    assert match_review("It doesn’t work at all", KW)["broken"] == ["doesn't work"]


def test_multiple_categories_and_phrases():
    found = match_review("Billing error after a price increase; support never replied.", KW)
    assert found["pricing"] == ["billing", "price increase"]
    assert found["broken"] == ["error"]
    assert found["support"] == ["never replied"]


def test_longest_phrase_wins():
    assert match_review("It slows down my store", KW)["performance"] == ["slows down"]


def test_non_english_text_has_no_flags():
    """D3: las reseñas en otros idiomas no se marcan, pero tampoco se descartan."""
    assert match_review("Pésima, hay que desinstalar muy seguido para que funcione", KW) == {}
