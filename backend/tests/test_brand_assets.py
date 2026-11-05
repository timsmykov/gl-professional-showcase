import re
from pathlib import Path
from xml.etree import ElementTree as ET

from gl_bot import copy

ASSETS = Path(__file__).resolve().parents[1] / "gl_bot/assets"


def test_avatar_keeps_every_original_contour_and_uses_uniform_scale():
    source = ET.parse(ASSETS / "logo-source.svg").getroot()
    avatar = ET.parse(ASSETS / "avatar.svg").getroot()
    ns = {"svg": "http://www.w3.org/2000/svg"}
    source_paths = [path.attrib["d"] for path in source.findall("svg:path", ns)]
    avatar_paths = avatar.findall("svg:g/svg:path", ns)
    assert source_paths and [path.attrib["d"] for path in avatar_paths] == source_paths
    assert all("transform" not in path.attrib for path in avatar_paths)
    transform = avatar.find("svg:g", ns).attrib["transform"]
    # A single scale factor preserves shape; independent X/Y scales and skew do not.
    assert re.fullmatch(r"translate\(248 175\) scale\(2\.2\) translate\(-301 -145\)", transform)
    assert avatar.attrib["viewBox"] == "0 0 1024 1024"


def test_profile_copy_fits_telegram_fields_without_claiming_live_purchase():
    assert len(copy.SHORT_DESCRIPTION) <= 120
    assert len(copy.DESCRIPTION) <= 512
    assert "ещё недоступны" in copy.DESCRIPTION
    assert len({command for command, _label in copy.COMMANDS}) == len(copy.COMMANDS)
