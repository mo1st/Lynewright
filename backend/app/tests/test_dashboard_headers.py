from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _location_block(conf: str) -> str:
    return conf[conf.index("    location / {"):]


def test_dashboard_sends_security_headers() -> None:
    block = _location_block((ROOT / "docker" / "dashboard.conf").read_text(encoding="utf-8"))
    for header in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert f"add_header {header}" in block
    assert "frame-ancestors 'none'" in block and "object-src 'none'" in block
    assert "'unsafe-inline'" not in block.split("script-src", 1)[1].split(";", 1)[0]


def test_csp_hash_matches_the_inline_theme_script() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1, "update the dashboard CSP when inline scripts change"
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode("ascii")
    conf = (ROOT / "docker" / "dashboard.conf").read_text(encoding="utf-8")
    assert f"'sha256-{digest}'" in conf
