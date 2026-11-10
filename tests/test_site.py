"""The landing page in site/: plain static files deployed to GitHub Pages.

A project page is served under /<repo>/, so a root-absolute link or asset
path would 404 in production while working on a local server. Every
reference must be relative and must resolve to a real file."""

import re
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"
HTML = (SITE / "index.html").read_text()


def _local_refs(text: str):
    for match in re.finditer(r'(?:href|src)="([^"]+)"', text):
        ref = match.group(1)
        if ref.startswith(("http://", "https://", "mailto:", "#", "data:")):
            continue
        yield ref


def test_the_page_has_its_basics():
    assert "<title>gitchen: git-based agent orchestration</title>" in HTML
    assert 'name="description"' in HTML
    assert "{{" not in HTML, "an unfilled template placeholder"
    assert "https://github.com/josei/gitchen" in HTML


def test_no_root_absolute_references():
    for ref in _local_refs(HTML):
        assert not ref.startswith("/"), f"{ref} would break under /gitchen/"
    for name in ("styles.css", "app.js"):
        assert not re.search(r'url\(\s*["\']?/', (SITE / name).read_text()), f"{name} has a root-absolute url()"


def test_every_local_reference_exists():
    for ref in _local_refs(HTML):
        path = SITE / ref.split("#")[0].split("?")[0]
        if path.is_dir():  # "./" is the site root: it must have an index page
            path = path / "index.html"
        assert path.is_file(), f"site/index.html points at {ref}, which is not in site/"
    css = (SITE / "styles.css").read_text()
    for ref in re.findall(r'url\(\s*["\']?([^"\')]+)', css):
        assert (SITE / ref).is_file(), f"styles.css points at {ref}, which is not in site/"


def test_the_bundled_font_ships_with_its_licence():
    assert (SITE / "fonts" / "fredoka.woff2").read_bytes()[:4] == b"wOF2"
    assert "SIL Open Font License" in (SITE / "fonts" / "OFL-Fredoka.txt").read_text()


def test_images_say_what_they_show():
    for tag in re.findall(r"<img\b[^>]*>", HTML):
        assert re.search(r'alt="[^"]+"', tag), f"image without alt text: {tag[:80]}"
