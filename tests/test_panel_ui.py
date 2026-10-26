"""CYBERBOX Trading Office front end: brand tokens and assets, strings, the Node unit tests of
the office engine, and the public assets of the login page. (The browser smoke test with
screenshots is tests/frontend_smoke.mjs, run by hand against `python -m panel --demo`.)"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mt5bot.team.roster import ROSTER

PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / "panel" / "static"
WEBSITE = PROJECT.parent / "cyberbox_website" / "public" / "index.html"
BRAND = {"teal": "#00C9A7", "blue": "#1874CD", "navy": "#1A3A6E", "red": "#E63030", "orange": "#F47C20",
         "yellow": "#FFE600", "cyan": "#00CFCF", "dark": "#080E1A", "darker": "#050A10", "text": "#E8EDF7",
         "muted": "#7A899E"}


def test_tokens_carry_the_cyberbox_brand_layer() -> None:
    tokens = (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")
    for name, value in BRAND.items():
        assert re.search(rf"--cbx-{name}:\s*{re.escape(value)};", tokens, re.IGNORECASE), name
    for theme in (':root[data-theme="light"]', ':root[data-contrast="high"]'):
        assert theme in tokens
    assert "Syne" in tokens and "Space Mono" in tokens


@pytest.mark.skipif(not WEBSITE.exists(), reason="CYBERBOX website sources not on this machine")
def test_brand_layer_matches_the_website_source() -> None:
    root = WEBSITE.read_text(encoding="utf-8", errors="replace")
    for name, value in BRAND.items():
        assert re.search(rf"--{name}:\s*{re.escape(value)}\s*;", root, re.IGNORECASE), name


def test_app_css_uses_tokens_not_raw_brand_hex() -> None:
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    raw = {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}\b", css)}
    assert not raw & {v.upper() for v in BRAND.values()}, "use var(--cbx-...) or a semantic token"


def test_fonts_are_vendored_with_their_licences() -> None:
    fonts_css = (STATIC / "css" / "fonts.css").read_text(encoding="utf-8")
    files = re.findall(r'url\("\.\./fonts/([^"]+)"\)', fonts_css)
    assert len(files) == 6
    for name in files:
        data = (STATIC / "fonts" / name).read_bytes()
        assert data[:4] == b"wOF2", name
    for licence in ("OFL-Syne.txt", "OFL-SpaceMono.txt"):
        assert "SIL OPEN FONT LICENSE" in (STATIC / "fonts" / licence).read_text(encoding="utf-8").upper()


def test_logo_and_favicon_are_local_pngs() -> None:
    for name in ("cyberbox-logo-256.png", "cyberbox-logo-96.png", "favicon-48.png"):
        assert (STATIC / "img" / name).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", name
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/static/img/cyberbox-logo-96.png" in index and 'alt="CYBERBOX"' in index
    for ref in re.findall(r'(?:src|href)="([^"#]+)"', index):
        assert ref.startswith("/static/"), ref
        assert (STATIC / ref[len("/static/"):]).exists(), ref
    assert (PROJECT / "panel" / "BRAND.md").exists()


def _dict(source: str, name: str) -> set[str]:
    start = source.index(f"const {name} = {{")
    end = source.index("\n};", start)
    return set(re.findall(r'"([a-z_][\w.]*)":', source[start:end]))


def test_every_literal_string_key_exists_in_portuguese() -> None:
    i18n = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    pt = _dict(i18n, "PT")
    en = _dict(i18n, "EN")
    used: set[str] = set()
    for path in (STATIC / "js").rglob("*.js"):
        used |= set(re.findall(r'\bt\("([a-z_][\w.]*)"', path.read_text(encoding="utf-8")))
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    used |= set(re.findall(r'data-i18n(?:-label|-title)?="([\w.]+)"', html))
    assert used - pt == set(), "missing PT-PT strings"
    assert en <= pt | {"app.title"}, "an English key without a Portuguese one"
    for prefix in ("tab.", "room.", "modechip.", "tier.", "session.short."):
        assert any(k.startswith(prefix) for k in en), prefix


def test_no_v2_or_engine_mode_labels_in_the_ui() -> None:
    for path in STATIC.rglob("*"):
        if path.suffix in (".js", ".html", ".css"):
            text = path.read_text(encoding="utf-8")
            assert "(v2)" not in text and "v2 mode" not in text.lower(), path


def test_portuguese_copy_has_no_em_dashes() -> None:
    i18n = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    pt = i18n[i18n.index("const PT = {"):i18n.index("const EN = {")]
    assert "—" not in pt
    assert "—" not in (PROJECT / "panel" / "BRAND.md").read_text(encoding="utf-8")


def test_node_roster_fixture_matches_the_real_roster() -> None:
    js = (PROJECT / "tests" / "js" / "office.test.mjs").read_text(encoding="utf-8")
    pairs = set(re.findall(r'\["([a-z_]+)", "([a-z_]+)"\]', js))
    assert pairs == {(a.id, a.room) for a in ROSTER}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_office_engine_unit_tests_pass_under_node() -> None:
    out = subprocess.run(["node", "--test", str(PROJECT / "tests" / "js" / "office.test.mjs")], capture_output=True,
                         text=True, timeout=120, cwd=PROJECT)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "# fail 0" in out.stdout


def _get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status, resp.getheader("Content-Type") or ""


def test_login_page_assets_are_public_but_nothing_else(tmp_path: Path) -> None:
    from mt5bot.config import Config
    from panel.app import run_in_thread, stop_server

    cfg = Config.from_dict({"state_root": str(tmp_path / "state"), "health_file": str(tmp_path / "health.json")})
    ctx, server, port = run_in_thread(cfg, token="t0ken-for-tests", remote=True)
    try:
        for path in ("/login", "/static/css/tokens.css", "/static/css/fonts.css", "/static/fonts/syne-latin.woff2",
                     "/static/img/cyberbox-logo-96.png", "/favicon.ico"):
            status, ctype = _get(port, path)
            assert status == 200, path
        assert _get(port, "/static/fonts/syne-latin.woff2")[1] == "font/woff2"
        for path in ("/", "/static/js/main.js", "/static/img/../js/main.js", "/api/status", "/api/funnel"):
            assert _get(port, path)[0] in (302, 401), path
    finally:
        stop_server(ctx, server)


def test_funnel_api_reports_counterfactuals(tmp_path: Path) -> None:
    from mt5bot.config import Config
    from mt5bot.db import db_path
    from mt5bot.registry import Registry
    from panel.app import run_in_thread, stop_server

    state = tmp_path / "state" / "SIM-1"
    Registry(db_path(state)).close()
    cfg = Config.from_dict({"state_root": str(tmp_path / "state"), "health_file": str(tmp_path / "health.json")})
    ctx, server, port = run_in_thread(cfg, state_dir=state)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/funnel?hours=24")
        body = json.loads(conn.getresponse().read())
        conn.close()
        assert "counterfactuals" in body and body["hours"] == 24 and "flow" in body
    finally:
        stop_server(ctx, server)
