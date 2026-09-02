"""Regression tests for the shared image-zoom overlay centering fix.

Bug: on both mobile and desktop, the zoomed-in overlay image did not appear
centered in the viewport. Root cause was overlay sizing/centering done via
CSS Grid with viewport-unit padding on the OVERLAY combined with percentage
max-width/max-height on the IMAGE -- an indirect chain that produced
inconsistent effective bounds across devices. Fixed by centering with flex
on the overlay and sizing the image directly against the viewport
(vw/vh, with a dvh override for mobile browser-chrome-safe height).

This is a single shared fix (image_zoom.js + this CSS block) used by every
page that reuses the "zoomable" component -- there is exactly one
.image-zoom-overlay/.image-zoom-overlay-image rule pair in app.css.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSS = (PROJECT_ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")
# Base (non-media-query) rules only -- media-query overrides are checked separately.
CSS_TOP_LEVEL = re.sub(r"@media\([^)]*\)\{.*?\}\}", "", CSS, flags=re.DOTALL)


def _rule(selector: str) -> str:
    matches = re.findall(re.escape(selector) + r"\{([^}]*)\}", CSS_TOP_LEVEL)
    assert matches, f"no top-level CSS rule found for {selector!r}"
    assert len(matches) == 1, f"expected exactly one top-level rule for {selector!r} (shared fix, no per-page duplicates), found {len(matches)}"
    return matches[0]


# ==================== 1. overlay fixed + inset:0 (full viewport) ====================

def test_overlay_is_fixed_and_covers_full_viewport():
    rule = _rule(".image-zoom-overlay")
    assert "position:fixed" in rule
    assert "inset:0" in rule


# ==================== 2. real flex/grid centering on the overlay ====================

def test_overlay_uses_flex_centering():
    rule = _rule(".image-zoom-overlay")
    assert "display:flex" in rule
    assert "align-items:center" in rule
    assert "justify-content:center" in rule


# ==================== 3. desktop size limits ====================

def test_overlay_image_desktop_size_limits():
    rule = _rule(".image-zoom-overlay-image")
    assert "max-width:90vw" in rule
    assert "max-height:90vh" in rule


# ==================== 4. mobile size limits (94vw / 90dvh with 90vh fallback) ====================

def test_overlay_image_mobile_size_limits_with_dvh_fallback():
    media_matches = re.findall(
        r"@media\(max-width:800px\)\{\.image-zoom-overlay-image\{([^}]*)\}\}", CSS,
    )
    assert media_matches, "no mobile (max-width:800px) override found for .image-zoom-overlay-image"
    mobile_rule = media_matches[0]
    assert "max-width:94vw" in mobile_rule
    # vh fallback must be declared BEFORE the dvh override so unsupported
    # browsers keep the vh value instead of an invalid/ignored declaration.
    vh_index = mobile_rule.index("max-height:90vh")
    dvh_index = mobile_rule.index("max-height:90dvh")
    assert vh_index < dvh_index


# ==================== 5. object-fit:contain, no stray sizing that could overflow ====================

def test_overlay_image_object_fit_contain_and_no_margin_overflow():
    rule = _rule(".image-zoom-overlay-image")
    assert "object-fit:contain" in rule
    assert "display:block" in rule
    assert "margin:0" in rule
