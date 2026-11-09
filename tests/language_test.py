from pdf2epub.utils.language import language_code


def test_language_code_maps_names_and_codes():
    assert language_code("English") == "en"
    assert language_code("German") == "de"
    assert language_code("Deutsch") == "de"
    assert language_code("French") == "fr"
    assert language_code("Chinese") == "zh"
    assert language_code("Japanese") == "ja"
    assert language_code("Original") == "en"
    assert language_code("zh-Hans") == "zh"
    assert language_code("") == "en"


def test_language_code_falls_back_for_unknown_names():
    assert language_code("Klingon") == "klingon"
    assert language_code("  ") == "en"
