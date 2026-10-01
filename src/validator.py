"""Kontrola TIFF souborů: přípona, formát, DPI, barevný režim, bitová hloubka, název (regex)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from config import COLOR_MODE_MAP, Config, Preset

# Skeny sbírek bývají obří – vypneme ochranu proti "decompression bomb".
Image.MAX_IMAGE_PIXELS = None

DPI_TOLERANCE = 0.5  # zaokrouhlení racionálních čísel v TIFF (např. 599.9998)

# Univerzální pravidlo názvu (bez přípony): <cokoli>-<inventární číslo>--<pořadí>
# Inventární číslo = úsek mezi posledním "-" a "--", např. ABC-01X-DIA001--001 -> DIA001.
FILENAME_PATTERN = re.compile(r"^(?P<prefix>.+)-(?P<inventory>[^-]+)--(?P<sequence>[^-]+)$")


@dataclass
class FileCheck:
    """Výsledek kontroly jednoho souboru (ukládá se 1:1 do JSON logu)."""

    filename: str
    path: str
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    format: str | None = None
    width: int | None = None
    height: int | None = None
    dpi_x: float | None = None
    dpi_y: float | None = None
    pillow_mode: str | None = None
    color_mode: str | None = None
    bits_per_sample: list[int] | None = None
    compression: str | None = None
    frames: int | None = None
    has_icc_profile: bool | None = None
    inventory: str | None = None
    regex_groups: dict[str, str] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchScan:
    """Obsah složky dávky rozdělený na TIFFy, ignorované a nepovolené položky."""

    tiffs: list[Path]
    ignored: list[Path]
    rejected: list[Path]


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def scan_batch_folder(folder: Path, cfg: Config) -> BatchScan:
    tiffs, ignored, rejected = [], [], []
    for entry in sorted(folder.iterdir(), key=lambda p: natural_key(p.name)):
        if entry.is_file() and entry.name.lower() in cfg.ignored_files:
            ignored.append(entry)
        elif entry.is_file() and entry.suffix.lower() in cfg.tiff_extensions:
            tiffs.append(entry)
        else:
            rejected.append(entry)
    return BatchScan(tiffs, ignored, rejected)


def _color_mode_name(pillow_mode: str) -> str:
    for name, modes in COLOR_MODE_MAP.items():
        if pillow_mode in modes:
            return name
    return pillow_mode


def check_filename(path: Path, cfg: Config, result: FileCheck) -> None:
    if path.suffix.lower() not in cfg.tiff_extensions:
        result.fail(f"Nepovolená přípona '{path.suffix}' (povoleno: {', '.join(sorted(cfg.tiff_extensions))}).")
        return
    match = FILENAME_PATTERN.fullmatch(path.stem)
    if not match:
        result.fail("Z názvu nelze určit inventární číslo – název musí mít tvar "
                    "<cokoli>-<inventární číslo>--<pořadí>, např. ABC-01X-DIA001--001.tif.")
        return
    result.regex_groups = match.groupdict()
    result.inventory = match["inventory"]


def check_image(path: Path, preset: Preset, result: FileCheck) -> None:
    try:
        result.size_bytes = path.stat().st_size
        with Image.open(path) as img:
            result.format = img.format
            if img.format != "TIFF":
                result.fail(f"Soubor není TIFF (Pillow rozpoznal formát '{img.format}').")
                return
            result.width, result.height = img.size
            result.pillow_mode = img.mode
            result.color_mode = _color_mode_name(img.mode)
            result.compression = img.info.get("compression")
            result.frames = getattr(img, "n_frames", 1)
            result.has_icc_profile = bool(img.info.get("icc_profile"))
            bps = img.tag_v2.get(258)  # BitsPerSample
            if bps is not None:
                result.bits_per_sample = [int(b) for b in (bps if isinstance(bps, tuple) else (bps,))]

            dpi = img.info.get("dpi")
            if dpi:
                result.dpi_x, result.dpi_y = round(float(dpi[0]), 3), round(float(dpi[1]), 3)

            if preset.deep_check:
                img.load()  # plné dekódování -> odhalí poškozená data
    except Exception as exc:  # noqa: BLE001 - chceme zachytit jakoukoli chybu čtení
        result.fail(f"Soubor nelze otevřít/přečíst jako obrázek: {type(exc).__name__}: {exc}")
        return

    if result.dpi_x is None or result.dpi_y is None:
        result.fail("Soubor neobsahuje informaci o rozlišení (DPI).")
    elif min(result.dpi_x, result.dpi_y) + DPI_TOLERANCE < preset.min_dpi:
        result.fail(
            f"Nízké rozlišení: {result.dpi_x:g}×{result.dpi_y:g} DPI, preset vyžaduje min. {preset.min_dpi:g} DPI."
        )

    if result.pillow_mode not in preset.pillow_modes():
        result.fail(
            f"Nepovolený barevný režim '{result.color_mode}' (Pillow: {result.pillow_mode}); "
            f"povoleno: {', '.join(preset.allowed_color_modes)}."
        )

    if preset.allowed_bit_depths and result.bits_per_sample:
        bad = [b for b in result.bits_per_sample if b not in preset.allowed_bit_depths]
        if bad:
            result.fail(
                f"Nepovolená bitová hloubka {result.bits_per_sample} bitů/kanál; povoleno: {preset.allowed_bit_depths}."
            )

    if preset.allowed_compressions and result.compression not in preset.allowed_compressions:
        result.fail(f"Nepovolená komprese '{result.compression}'; povoleno: {preset.allowed_compressions}.")

    if result.frames and result.frames > 1:
        result.warnings.append(f"TIFF obsahuje {result.frames} snímků/stránek (např. náhled) – zkontrolujte.")


def validate_file(path: Path, preset: Preset, cfg: Config) -> FileCheck:
    result = FileCheck(filename=path.name, path=str(path))
    check_filename(path, cfg, result)
    check_image(path, preset, result)
    return result
