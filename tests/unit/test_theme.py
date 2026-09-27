"""Brand theme: wordmark shape, palette use, plain/report rendering."""

from __future__ import annotations

from harness import theme


def test_wordmark_is_a_compact_banner():
    lines = theme.logo_lines()
    assert 1 <= len(lines) <= 6
    assert max(len(line) for line in lines) <= 72  # report header / 80-col terminal
    assert all(line.strip() for line in lines)


def test_wordmark_uses_readable_brand_colors_and_strips_cleanly():
    markup = theme.logo_markup()
    # the two-tone wordmark must use the text-safe colours (readable on dark)
    assert theme.BRAND_SKY in markup and theme.BRAND_MID in markup
    plain = theme.logo_plain()
    assert "[" not in plain and "#" not in plain
    assert plain.splitlines() == theme.logo_lines()


def test_header_panel_carries_brand_title_and_subtitle():
    panel = theme.header_panel()
    assert panel is not None
    assert theme.BRAND_TITLE in panel.title
    assert theme.BRAND_BORDER in str(panel.border_style)


def test_status_style_reserves_green_and_red_for_pass_fail():
    assert theme.status_style("submitted") == theme.BRAND_PASS
    assert theme.status_style("error") == theme.BRAND_FAIL
    assert theme.status_style("limits_exceeded") == theme.BRAND_FAIL
    assert theme.status_style("running") == theme.BRAND_BRIGHT
