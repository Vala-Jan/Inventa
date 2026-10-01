import pytest
from conftest import PRESET, batch_dir, make_tiff

from validator import validate_file


def check(cfg, name, **kw):
    path = make_tiff(batch_dir(cfg) / name, **kw)
    return validate_file(path, cfg.presets[PRESET], cfg)


def test_valid_rgb_file(workspace):
    r = check(workspace, "ABC-01X-DIA001--001.tif")
    assert r.ok, r.errors
    assert r.inventory == "DIA001"
    assert r.regex_groups["prefix"] == "ABC-01X"
    assert r.regex_groups["sequence"] == "001"
    assert (r.dpi_x, r.dpi_y) == (600, 600)


def test_grayscale_16bit_and_uppercase_ext(workspace):
    r = check(workspace, "ABC-01X-DIA002--002.TIF", mode="I;16")
    assert r.ok, r.errors
    assert r.color_mode == "Grayscale"
    assert r.bits_per_sample == [16]


def test_low_dpi_fails(workspace):
    r = check(workspace, "ABC-01X-DIA001--001.tif", dpi=(300, 300))
    assert not r.ok and "Nízké rozlišení" in r.errors[0]


def test_dpi_rounding_tolerated(workspace):
    assert check(workspace, "ABC-01X-DIA001--001.tif", dpi=(599.9998, 599.9998)).ok


def test_bad_name_fails(workspace):
    r = check(workspace, "ABC-01X-DIA001-001.tif")
    assert not r.ok and "inventární číslo" in r.errors[0]
    assert r.inventory is None


def test_wrong_color_mode_fails(workspace):
    r = check(workspace, "ABC-01X-DIA001--001.tif", mode="CMYK")
    assert not r.ok and "barevný režim" in r.errors[0]


def test_jpeg_renamed_to_tif_fails(workspace):
    from PIL import Image

    path = batch_dir(workspace) / "ABC-01X-DIA001--001.tif"
    Image.new("RGB", (10, 10)).save(path, format="JPEG", dpi=(600, 600))
    r = validate_file(path, workspace.presets[PRESET], workspace)
    assert not r.ok and "není TIFF" in r.errors[0]


def test_wrong_extension_fails(workspace):
    path = batch_dir(workspace) / "ABC-01X-DIA001--001.jpg"
    path.write_bytes(b"x")
    r = validate_file(path, workspace.presets[PRESET], workspace)
    assert not r.ok and "přípona" in r.errors[0]


@pytest.mark.parametrize("stem, inventory", [
    ("ABC-01X-DIA001--001", "DIA001"),
    ("ABC-01X-FOT123--002", "FOT123"),
    ("ABC-NEG55--1", "NEG55"),
    ("X-Y-Z-K12a--0003", "K12a"),
])
def test_universal_inventory_rule(workspace, stem, inventory):
    r = check(workspace, f"{stem}.tif")
    assert r.ok, r.errors
    assert r.inventory == inventory


@pytest.mark.parametrize("stem", ["DIA001--001", "ABC-01X-DIA001-001", "ABC-01X-DIA001---001", "ABC-01X---001"])
def test_names_without_inventory_fail(workspace, stem):
    r = check(workspace, f"{stem}.tif")
    assert not r.ok and "inventární číslo" in r.errors[0]
