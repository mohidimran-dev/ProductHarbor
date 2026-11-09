"""Interactive scrape image cache for protected brand CDNs."""
from __future__ import annotations

from pathlib import Path

from shopextract.dashboard.image_assets import read_image_cache, write_image_cache

PNG_1X1 = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


def test_image_cache_roundtrip(tmp_path: Path):
    url = 'https://cdn.example/a/b.png?wid=100&utm_source=x'
    path = write_image_cache(tmp_path, url, PNG_1X1)
    assert path is not None and path.is_file()
    loaded = read_image_cache(tmp_path, 'https://cdn.example/a/b.png?wid=100')
    assert loaded == PNG_1X1
    assert read_image_cache(tmp_path, 'https://cdn.example/missing.png') is None


def test_image_cache_rejects_html(tmp_path: Path):
    assert write_image_cache(tmp_path, 'https://cdn.example/x.jpg', b'<html>nope</html>') is None
