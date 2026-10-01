import json
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from config import load_config  # noqa: E402

PRESET = "Diapozitivy 600 DPI"


@pytest.fixture
def workspace(tmp_path):
    """Kopie struktury D:\\Digitalizační_pracoviště v dočasné složce (včetně diakritiky)."""
    root = tmp_path / "Digitalizační_pracoviště"
    app = root / "TOOLS" / "Inventa"
    (app / "config").mkdir(parents=True)
    data = json.loads((REPO / "config" / "presets.json").read_text(encoding="utf-8"))
    data["paths"]["scans_root"] = str(root / "SCANS" / "MUZEUM" / "Pracovníci")
    data["paths"]["archive_root"] = str(root / "Z_archiv" / "Digitalizace")
    data["paths"]["exiftool"] = "tools/neexistuje.exe"  # -> použije exiftool z PATH
    (app / "config" / "presets.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (root / "SCANS" / "MUZEUM" / "Pracovníci").mkdir(parents=True)
    (root / "Z_archiv" / "Digitalizace").mkdir(parents=True)
    (app / "PREPARED").mkdir()
    return load_config(app / "config" / "presets.json", app_root=app)


def make_tiff(path: Path, dpi=(600, 600), mode="RGB", size=(64, 48), **kw):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color=0).save(path, format="TIFF", dpi=dpi, **kw)
    return path


def batch_dir(cfg, worker="Jana Nováková", batch="2026-09-29"):
    d = cfg.scans_root / worker / batch
    d.mkdir(parents=True, exist_ok=True)
    return d


requires_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="ExifTool není nainstalován")
