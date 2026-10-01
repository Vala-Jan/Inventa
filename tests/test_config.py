import copy
import json

import pytest

from config import ConfigError, load_config


def write(tmp_path, data):
    p = tmp_path / "presets.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


BASE = {
    "paths": {"scans_root": "s", "prepared": "p", "logs": "l", "archive_root": "Z:/Digitalizace"},
    "presets": {"X": {"min_dpi": 600, "allowed_color_modes": ["RGB"],
                      "metadata": {"XMP-dc:Identifier": "{inventory}"}}},
}


def test_repo_config_loads():
    cfg = load_config()
    assert "Diapozitivy 600 DPI" in cfg.presets


def test_windows_absolute_path_kept(tmp_path):
    cfg = load_config(write(tmp_path, BASE), app_root=tmp_path)
    assert str(cfg.archive_root).startswith("Z:")


@pytest.mark.parametrize("patch, msg", [
    ({"allowed_color_modes": ["Sepia"]}, "Neznámý barevný režim"),
    ({"min_dpi": 0}, "min_dpi"),
    ({"metadata": {"bad tag": "x"}}, "Neplatný název tagu"),
])
def test_invalid_presets(tmp_path, patch, msg):
    data = copy.deepcopy(BASE)
    data["presets"]["X"].update(patch)
    with pytest.raises(ConfigError, match=msg):
        load_config(write(tmp_path, data), app_root=tmp_path)


def test_invalid_json(tmp_path):
    p = tmp_path / "presets.json"
    p.write_text("{ nope", encoding="utf-8")
    with pytest.raises(ConfigError, match="není platný JSON"):
        load_config(p, app_root=tmp_path)
