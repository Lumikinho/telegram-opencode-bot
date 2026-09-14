"""CHANGELOG.md acompanha a versão do pacote híbrido."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_changelog_cita_versao_do_package():
    pkg = json.loads((ROOT / "package.json").read_text())
    md = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^##\s+(.+)$", md, re.M)
    assert headings, "CHANGELOG sem seções ##"
    assert pkg["version"] in headings[0], f"topo {headings[0]!r} != {pkg['version']}"
    assert pkg["version"] in md
