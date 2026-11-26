from pdf2epub.special_role_audit import (
    _index_entry_groups,
    validate_special_role_markers,
)


def test_index_parser_uses_page_anchors_despite_translation_indentation():
    source = "Alpha 10\nBeta 20\nGamma30"
    target = "甲10\n 乙20\n 丙30"

    errors = validate_special_role_markers(source, target, "index")
    assert len(errors) >= 1
    assert "indentation hierarchy changed" in errors[0]
    assert "entry count mismatch" not in errors[0]
    assert any("index numeric marker mismatch" in error for error in errors)
    entries = _index_entry_groups(target)
    assert [(entry.start_line, entry.end_line, entry.indent, entry.markers) for entry in entries] == [
        (1, 1, 0, ["10"]),
        (2, 2, 1, ["20"]),
        (3, 3, 1, ["30"]),
    ]


def test_index_parser_keeps_unpaged_cross_references_as_nested_entries():
    source = "Alpha\n  see Beta\nGamma 30"
    target = "甲\n  另见乙\n丙 30"

    assert not validate_special_role_markers(source, target, "index")
    assert len(_index_entry_groups(source)) == 3
    assert len(_index_entry_groups(target)) == 3


def test_index_parser_preserves_real_nested_entry_hierarchy():
    source = "Animals 10\n  Cats 11\n    Persian 12"
    target = "动物 10\n  猫科 11\n    波斯猫 12"

    assert not validate_special_role_markers(source, target, "index")


def test_index_parser_pairs_unpaged_wraps_without_language_length_heuristics():
    source = "Alpha 10\n  short wrap\nBeta 20"
    target = "甲 10\n  这是一段明显更长的中文续排内容，但没有单独的页码或交叉引用锚点\n乙 20"

    assert not validate_special_role_markers(source, target, "index")
    entries = _index_entry_groups(source)
    assert len(entries) == 2
    assert (entries[0].start_line, entries[0].end_line, entries[0].text) == (
        1, 2, "Alpha 10 short wrap"
    )


def test_index_allows_translated_multi_line_indented_continuation():
    source = "Alpha 10\nBeta 20"
    target = "甲 10\n  这一项的续排说明\n乙 20"

    assert not validate_special_role_markers(source, target, "index")
    entries = _index_entry_groups(target)
    assert len(entries) == 2
    assert entries[0].end_line == 2
    assert entries[0].text == "甲 10 这一项的续排说明"


def test_index_reports_missing_cross_reference_entry():
    errors = validate_special_role_markers(
        "Alpha 10\n  see Beta\nGamma 20",
        "甲 10\n乙 20",
        "index",
    )

    assert errors
    assert "cross-reference" in errors[0]
    assert "source line 2" in errors[0]


def test_index_explicit_nested_list_entry_is_not_folded_as_wrap():
    source = "Alpha 10\n  - child term\nBeta 20"
    target = "甲 10\n  - 子条目\n乙 20"

    assert not validate_special_role_markers(source, target, "index")
    assert len(_index_entry_groups(source)) == 3


def test_index_parser_attaches_page_only_line_to_previous_entry():
    entries = _index_entry_groups("Alpha 10\n  20, 21\nBeta 30")

    assert len(entries) == 2
    assert entries[0].markers == ["10", "20", "21"]
    assert entries[0].end_line == 2


def test_index_page_locator_mapping_change_is_reported_at_the_affected_rows():
    errors = validate_special_role_markers(
        "Alpha 10\nBeta 20",
        "甲 11\n乙 20",
        "index",
    )

    assert errors
    assert "numeric marker mismatch" in errors[0]
    assert "source line 1" in errors[0]
    assert "target line 1" in errors[0]


def test_index_term_digits_and_years_are_not_promoted_to_page_cues():
    errors = validate_special_role_markers(
        "COVID19 and 3D models, 2020",
        "COVID19 与 3D 模型，2020",
        "index",
    )

    assert not errors


def test_index_detects_deleted_prose_only_top_level_term():
    errors = validate_special_role_markers(
        "Alpha\nBeta 20",
        "乙 20",
        "index",
    )

    assert errors
    assert "entry count mismatch" in errors[0]
    assert "source line 1" in errors[0]


def test_index_merge_diagnostic_reports_line_ranges_and_nearby_terms():
    errors = validate_special_role_markers(
        "Alpha 10\nBeta 20\nGamma 30",
        "甲10；乙20；丙30",
        "index",
    )

    assert len(errors) == 1
    assert "suspected merge" in errors[0]
    assert "source line 2" in errors[0]
    assert "target line 1" in errors[0]
    assert "Beta 20" in errors[0]
    assert "甲10" in errors[0]


def test_index_merge_diagnostic_points_to_late_first_boundary_drift():
    source_lines = [f"Term {index} {index}" for index in range(1, 101)]
    target_lines = source_lines.copy()
    target_lines[86:88] = ["术语87 87；术语88 88"]
    errors = validate_special_role_markers(
        "\n".join(source_lines), "\n".join(target_lines), "index"
    )

    assert errors
    assert "source line 88" in errors[0]
    assert "target line 87" in errors[0]


def test_bibliography_accepts_prose_range_translated_with_dash():
    assert not validate_special_role_markers(
        "# Scritti dal 1945 al 1970\n",
        "# 1945—1970年著作全目\n",
        "bibliography",
    )
    assert not validate_special_role_markers(
        "pp. 12–15\n",
        "第12至15页\n",
        "bibliography",
    )
    assert validate_special_role_markers(
        "# Scritti dal 1945 al 1970\n",
        "# 1945—1971年著作全目\n",
        "bibliography",
    )


def test_range_boundary_alteration_is_rejected_for_index_and_bibliography():
    for role in ("index", "bibliography"):
        assert validate_special_role_markers(
            "12, 15–20\n",
            "12–15, 20\n",
            role,
        )
        assert validate_special_role_markers(
            "12–15\n",
            "12, 15\n",
            role,
        )


def test_bibliography_accepts_natural_chinese_dates_and_issues_and_rejects_mismatches():
    assert not validate_special_role_markers(
        "25 giugno 1945\n",
        "1945年6月25日\n",
        "bibliography",
    )
    assert validate_special_role_markers(
        "25 giugno 1945\n",
        "1945年7月25日\n",
        "bibliography",
    )
    assert not validate_special_role_markers(
        "Prometeo, n. 5, 1952, p. 10\n",
        "《普罗米修斯》，1952年第5期，第10页\n",
        "bibliography",
    )
    assert validate_special_role_markers(
        "Prometeo, n. 5, 1952, p. 10\n",
        "《普罗米修斯》，1952年第6期，第10页\n",
        "bibliography",
    )


def test_long_bibliography_mismatch_reports_source_and_target_line_numbers():
    source_lines = [f"Entry {i}, 1950" for i in range(1, 51)]
    target_lines = [f"条目 {i}，1950" for i in range(1, 51)]
    target_lines[44] = "条目 45，1951"

    errors = validate_special_role_markers(
        "\n".join(source_lines),
        "\n".join(target_lines),
        "bibliography",
    )

    assert len(errors) == 1
    assert "source line 45" in errors[0]
    assert "target line 45" in errors[0]
    assert "1950" in errors[0] and "1951" in errors[0]
