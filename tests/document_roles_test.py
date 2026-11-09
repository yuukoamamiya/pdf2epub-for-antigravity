import pytest

from pdf2epub.document_roles import infer_translation_file_role, is_bibliography_text


@pytest.mark.parametrize("heading", [
    "3. Primary literature", "## 4) Secondary literature", "# Primärliteratur",
    "5. Literaturverzeichnis", "3. Bibliografía", "Works Cited",
    "Nachweise", "Quellennachweise", "Textnachweise",
])
def test_standalone_numbered_multilingual_reference_headings_are_bibliography(heading):
    assert is_bibliography_text(heading + "\n\nSmith, John. Book, 1999.")
    assert infer_translation_file_role("unit.md", heading + "\n\nSmith, John. Book, 1999.") == "bibliography"


def test_humanities_city_publisher_year_entries_identify_continuation_page():
    entries = "\n\n".join([
        "Adorno, Theodor. Aesthetic Theory. London: Routledge, 1984.",
        "Benjamin, Walter. The Arcades Project. Cambridge: Harvard University Press, 1999.",
        "Butler, Judith. Gender Trouble. New York: Routledge, 1990.",
        "Foucault, Michel. Discipline and Punish. New York: Vintage Books, 1977.",
        "Hegel, G. W. F. Phenomenology of Spirit. Oxford: Oxford University Press, 1977.",
    ])
    assert is_bibliography_text(entries)
    assert infer_translation_file_role("chapter_8.2.part2.md", entries) == "bibliography"


def test_parenthesized_city_publisher_and_year_suffix_entries_identify_references():
    entries = "\n\n".join([
        "Adorno, Theodor. Aesthetic Theory (London: Routledge, 1984a).",
        "Benjamin, Walter. The Arcades Project (Cambridge: Harvard University Press, 1999b).",
        "Butler, Judith. Gender Trouble (New York: Routledge, 1990a).",
        "Foucault, Michel. Discipline and Punish (New York: Vintage Books, 1977a).",
        "Hegel, G. W. F. Phenomenology of Spirit (Oxford: Oxford University Press, 1977b).",
    ])
    assert is_bibliography_text(entries)


def test_repeated_running_prose_with_years_and_doi_urls_is_not_bibliography():
    text = "\n\n".join(
        f"Following Smith ({1990 + i}), we develop the philosophical argument in detail "
        f"rather than list a reference. See https://doi.org/10.1000/example{i} for the discussion."
        for i in range(5)
    )
    assert not is_bibliography_text(text)
    assert infer_translation_file_role("chapter_8.md", text) is None


def test_reference_word_inside_a_prose_heading_is_not_a_standalone_section():
    assert not is_bibliography_text("# References to Hegel in the argument\n\nOrdinary prose follows.")


@pytest.mark.parametrize("text", [
    "The years 1990, 1999, 2001, 2005, and 2010 mark stages in the argument.",
    "The author cites many studies (1990), (1999), and (2005) in this discussion.\n\n"
    "The literature includes several URLs: https://example.com/a, https://example.com/b, "
    "https://example.com/c, https://example.com/d, and https://example.com/e.",
    "A long analytical paragraph gives five discussions of books, pages, years, and argument structure.\n\n"
    "Another prose paragraph mentions 1999 and a DOI URL https://doi.org/10.1000/example.\n\n"
    "More body text gives examples, citations, and discussion without separate reference entries.\n\n"
    "The next paragraph cites scholarship in running prose.\n\n"
    "Final paragraph concludes the argument.",
])
def test_years_citations_and_urls_in_prose_do_not_exempt_unit(text):
    assert not is_bibliography_text(text)
    assert infer_translation_file_role("chapter_8.md", text) is None


def test_explicit_structural_role_beats_content_inference():
    assert infer_translation_file_role("chapter_index.md", "Ordinary body text.") == "index"
