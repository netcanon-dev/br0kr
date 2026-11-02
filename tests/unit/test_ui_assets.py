"""Unit tests for the vendored UI assets (br0kr/ui_assets.py): the embedded payloads still hash
to the sha256s recorded from netcanon-dev/ui-design-spec at the pinned tag, and the asset table +
the dashboard wiring agree (versioned paths, route tag, <head> references, dark-first default).
The payloads live INSIDE Python string literals where no linter can see a corrupted byte — the
hash is the only honest gate (the same doctrine as the PAGE backtick-parity test in
tests/unit/test_dashboard.py)."""
from __future__ import annotations

import hashlib

import pytest

from br0kr import dashboard, ui_assets

pytestmark = pytest.mark.unit

# sha256 of each vendored dist file at netcanon-dev/ui-design-spec tag v0.2.1
# (commit 1c4af32911e0ddc370a4a3049112adb4e35e8f7f). On upgrade: update the payloads AND these
# hashes in the same commit — this test is what makes "pinned" mean something.
_PINNED = {
    "NETCANON_UI_CSS": "0cc326194373fa6ba8d19a281e0946043a6c056a1dbe9b6ec9bbd0940fe40e68",
    "BR0KR_COMPAT_CSS": "6848cf34418389a35ff1edcea7ec3dc570095ef9f0037db7f17decfe583d8747",
    "THEME_PICKER_JS": "eb49114e4e921abb749490400d3085af93d86042f0f135031214c8bfe4bc69a8",
}


def test_vendored_payloads_match_the_pinned_spec_sha256s():
    """Each embedded payload hashes to the sha256 recorded from the spec repo at the pinned tag.
    Guards ANY byte drift in the vendored constants — a hand-edit, an editor re-wrap, a stripped
    trailing space — which would silently fork br0kr off the deliverable it claims to pin."""
    for name, want in _PINNED.items():
        got = hashlib.sha256(getattr(ui_assets, name).encode()).hexdigest()
        assert got == want, f"{name} drifted from the pinned spec bytes (see br0kr/ui_assets.py header)"


def test_asset_table_paths_route_and_the_page_references_them():
    """Every ASSETS path embeds SPEC_TAG (the cache-buster the immutable Cache-Control depends
    on), resolves through dashboard.route() to the 'asset' tag, and appears verbatim in the PAGE
    <head>. Guards a route/PAGE mismatch (a 404'd stylesheet = an unthemed dashboard) and an
    asset URL escaping the versioned namespace."""
    assert set(_PINNED) == {"NETCANON_UI_CSS", "BR0KR_COMPAT_CSS", "THEME_PICKER_JS"}
    assert len(ui_assets.ASSETS) == 3
    for path, (ctype, body) in ui_assets.ASSETS.items():
        assert path.startswith(f"/assets/{ui_assets.SPEC_TAG}/")
        assert dashboard.route(path) == "asset"
        assert isinstance(body, bytes) and body
        assert ctype in ("text/css; charset=utf-8", "text/javascript; charset=utf-8")
        assert path in dashboard.PAGE


def test_page_ships_dark_first_ocean_and_the_compact_picker():
    """The <html> tag carries the shim's dark-first doctrine (data-nc-theme="ocean"
    data-nc-mode="dark" — light mode stays blocked by the PAGE's hardcoded dark literals until
    the named follow-up migrates them) and the header carries the COMPACT picker (swatches only:
    no Light/Auto buttons to walk a user into the broken light mode) inside a COLLAPSIBLE <details>
    disclosure (the swatches show only when the "Theme" summary is opened — a popover that never
    reflows the header). Guards the default flipping, the full picker landing before the literal
    migration, or the picker escaping its collapsible wrapper back into always-on header clutter."""
    assert '<html lang="en" data-nc-theme="ocean" data-nc-mode="dark">' in dashboard.PAGE
    # the compact picker is WRAPPED in the collapsible disclosure (not bare in the header)
    assert '<details id="theme-menu"><summary>Theme</summary><nc-theme-picker compact></nc-theme-picker></details>' \
        in dashboard.PAGE
