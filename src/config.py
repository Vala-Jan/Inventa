"""Načtení a kontrola konfigurace z config/presets.json."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_ROOT / "config" / "presets.json"
# Cesty, které si uživatel nastaví v aplikaci. Leží vedle presets.json, ale není v gitu,
# takže je aktualizace aplikace nepřepíše.
SETTINGS_NAME = "settings.json"

# Přátelské názvy barevných režimů -> režimy Pillow
COLOR_MODE_MAP = {
    "RGB": {"RGB"},
    "Grayscale": {"L", "I;16", "I;16L", "I;16B", "I;16N", "I"},
    "Bitonal": {"1"},
    "CMYK": {"CMYK"},
    "RGBA": {"RGBA"},
}


class ConfigError(Exception):
    """Chyba v konfiguraci (presets.json)."""


@dataclass
class Preset:
    name: str
    description: str
    min_dpi: float
    allowed_color_modes: list[str]
    metadata: dict[str, str]
    allowed_bit_depths: list[int] | None = None
    allowed_compressions: list[str] | None = None
    deep_check: bool = False
    raw: dict = field(default_factory=dict)

    def pillow_modes(self) -> set[str]:
        modes: set[str] = set()
        for name in self.allowed_color_modes:
            modes |= COLOR_MODE_MAP[name]
        return modes


@dataclass
class Config:
    app_root: Path  # relativní cesty se počítají od této složky
    scans_root: Path | None  # None = uživatel cestu zatím nenastavil
    prepared: Path
    logs: Path
    archive_root: Path | None
    exiftool_configured: Path
    ignored_files: set[str]
    tiff_extensions: set[str]
    transfer_mode: str
    archive_subpath: str
    robocopy_retries: int
    robocopy_wait_seconds: int
    presets: dict[str, Preset]
    source_path: Path
    paths_raw: dict[str, str]  # cesty tak, jak jsou zapsané (pro okno Nastavení cest)

    def exiftool_path(self) -> str | None:
        """Cesta k ExifToolu: lokální tools/exiftool.exe, jinak 'exiftool' v PATH."""
        if self.exiftool_configured.is_file():
            return str(self.exiftool_configured)
        return shutil.which("exiftool")

    def missing_paths(self) -> list[str]:
        """Povinné cesty, které uživatel ještě nenastavil."""
        return [key for key in ("scans_root", "archive_root") if getattr(self, key) is None]


def _resolve(app_root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (app_root / p).resolve()


def _resolve_optional(app_root: Path, value: str) -> Path | None:
    return _resolve(app_root, value) if value and value.strip() else None


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"V konfiguraci chybí klíč '{key}' ({where}).")
    return d[key]


def _parse_preset(name: str, d: dict) -> Preset:
    where = f"preset '{name}'"
    min_dpi = _require(d, "min_dpi", where)
    if not isinstance(min_dpi, (int, float)) or min_dpi <= 0:
        raise ConfigError(f"'min_dpi' musí být kladné číslo ({where}).")

    modes = _require(d, "allowed_color_modes", where)
    unknown = [m for m in modes if m not in COLOR_MODE_MAP]
    if not modes or unknown:
        raise ConfigError(
            f"Neznámý barevný režim {unknown} ({where}). Povolené: {', '.join(COLOR_MODE_MAP)}."
        )

    metadata = _require(d, "metadata", where)
    if not isinstance(metadata, dict) or not metadata:
        raise ConfigError(f"'metadata' musí být neprázdný objekt tag -> šablona ({where}).")
    for tag in metadata:
        if not re.fullmatch(r"[A-Za-z0-9-]+:[A-Za-z0-9]+", tag):
            raise ConfigError(f"Neplatný název tagu '{tag}' ({where}), očekáváno např. XMP-dc:Identifier.")

    return Preset(
        name=name,
        description=d.get("description", ""),
        min_dpi=float(min_dpi),
        allowed_color_modes=list(modes),
        metadata=dict(metadata),
        allowed_bit_depths=d.get("allowed_bit_depths"),
        allowed_compressions=d.get("allowed_compressions"),
        deep_check=bool(d.get("deep_check", False)),
        raw=d,
    )


def _load_settings(settings_path: Path) -> dict:
    if not settings_path.is_file():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{settings_path.name} není platný JSON (řádek {exc.lineno}): {exc.msg}") from exc


def save_user_paths(config_path: Path | str, paths: dict[str, str]) -> Path:
    """Uloží cesty z okna Nastavení cest. Prázdná hodnota = výchozí hodnota z presets.json."""
    settings_path = Path(config_path).parent / SETTINGS_NAME
    data = _load_settings(settings_path)
    merged = {**data.get("paths", {}), **paths}
    data["paths"] = {key: value for key, value in merged.items() if value.strip()}
    settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return settings_path


def load_config(path: Path | str = DEFAULT_CONFIG_PATH, app_root: Path | None = None) -> Config:
    path = Path(path)
    # relativní cesty se počítají od složky aplikace = rodič složky config/
    app_root = Path(app_root) if app_root else path.resolve().parent.parent
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Konfigurační soubor nenalezen: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"presets.json není platný JSON (řádek {exc.lineno}, sloupec {exc.colno}): {exc.msg}") from exc

    paths = {**_require(data, "paths", "kořen"), **_load_settings(path.parent / SETTINGS_NAME).get("paths", {})}
    batch = data.get("batch", {})
    transfer = data.get("transfer", {})

    mode = transfer.get("mode", "verify_then_delete")
    if mode not in ("verify_then_delete", "robocopy_move"):
        raise ConfigError("transfer.mode musí být 'verify_then_delete' nebo 'robocopy_move'.")

    presets_raw = _require(data, "presets", "kořen")
    if not presets_raw:
        raise ConfigError("V konfiguraci není definován žádný preset.")
    presets = {name: _parse_preset(name, d) for name, d in presets_raw.items()}

    return Config(
        app_root=app_root,
        scans_root=_resolve_optional(app_root, paths.get("scans_root", "")),
        prepared=_resolve(app_root, _require(paths, "prepared", "paths")),
        logs=_resolve(app_root, _require(paths, "logs", "paths")),
        archive_root=_resolve_optional(app_root, paths.get("archive_root", "")),
        exiftool_configured=_resolve(app_root, paths.get("exiftool", "tools/exiftool.exe")),
        ignored_files={n.lower() for n in batch.get("ignored_files", [])},
        tiff_extensions={e.lower() for e in batch.get("tiff_extensions", [".tif", ".tiff"])},
        transfer_mode=mode,
        archive_subpath=transfer.get("archive_subpath", "{worker}/{batch}"),
        robocopy_retries=int(transfer.get("robocopy_retries", 2)),
        robocopy_wait_seconds=int(transfer.get("robocopy_wait_seconds", 5)),
        presets=presets,
        source_path=path,
        paths_raw={key: str(value) for key, value in paths.items()},
    )
