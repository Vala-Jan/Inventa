"""Zápis a ověření Dublin Core metadat přes ExifTool.

Důležité: cesta 'D:\\Digitalizační_pracoviště' obsahuje diakritiku. ExifTool na Windows
nepřebírá Unicode argumenty z příkazové řádky spolehlivě, proto se všechny argumenty
(včetně cest) předávají přes UTF-8 argfile (-@) spolu s '-charset filename=utf8'.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from config import Preset

EXIFTOOL_TIMEOUT = 300  # s, velké TIFFy na pomalém disku
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # bez blikajícího okna na Windows


class MetadataError(Exception):
    pass


@dataclass
class ExifToolRun:
    """Záznam o jednom spuštění ExifToolu (ukládá se do JSON logu)."""

    command: list[str]
    argfile_args: list[str]
    command_line: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MetadataResult:
    values: dict[str, str]
    run: ExifToolRun
    verified: bool = False


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def run_exiftool(exiftool: str, args: list[str]) -> ExifToolRun:
    fd, argfile = tempfile.mkstemp(prefix="exiftool_", suffix=".args")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(args) + "\n")
        base = [exiftool, "-charset", "filename=utf8"]
        command = [*base, "-@", argfile]
        run = ExifToolRun(
            command=command,
            argfile_args=list(args),
            command_line=" ".join(_quote(a) for a in [*base, *args]),
        )
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                timeout=EXIFTOOL_TIMEOUT,
                creationflags=NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise MetadataError(f"ExifTool nenalezen: {exiftool}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MetadataError(f"ExifTool nedoběhl do {EXIFTOOL_TIMEOUT} s.") from exc
        run.returncode = proc.returncode
        run.stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        run.stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        return run
    finally:
        try:
            os.remove(argfile)
        except OSError:
            pass


def exiftool_version(exiftool: str | None) -> str | None:
    if not exiftool:
        return None
    try:
        proc = subprocess.run(
            [exiftool, "-ver"], capture_output=True, timeout=60, creationflags=NO_WINDOW
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("ascii", errors="replace").strip() or None


def render_values(preset: Preset, context: dict[str, str]) -> dict[str, str]:
    """Doplní šablony z presetu (např. '{inventory}') hodnotami ze jména souboru."""
    values = {}
    for tag, template in preset.metadata.items():
        try:
            value = template.format(**context)
        except (KeyError, IndexError, ValueError) as exc:
            raise MetadataError(f"Šablonu '{template}' pro {tag} nelze vyplnit: {exc}") from exc
        if "\n" in value or "\r" in value:
            raise MetadataError(f"Hodnota pro {tag} obsahuje zalomení řádku – nepovoleno.")
        values[tag] = value
    return values


def write_metadata(exiftool: str, path: Path, values: dict[str, str]) -> ExifToolRun:
    args = ["-overwrite_original", "-P", "-m"]
    args += [f"-{tag}={value}" for tag, value in values.items()]
    args.append(str(path))
    run = run_exiftool(exiftool, args)
    if run.returncode != 0 or "1 image files updated" not in run.stdout:
        raise MetadataError(
            f"ExifTool selhal (návratový kód {run.returncode}): {run.stderr or run.stdout or 'bez výstupu'}"
        )
    return run


def read_metadata(exiftool: str, path: Path, tags: list[str]) -> dict[str, str]:
    args = ["-j", "-G1", *[f"-{t}" for t in tags], str(path)]
    run = run_exiftool(exiftool, args)
    if run.returncode != 0:
        raise MetadataError(f"Zpětné čtení metadat selhalo: {run.stderr or run.stdout}")
    try:
        data = json.loads(run.stdout)[0]
    except (json.JSONDecodeError, IndexError) as exc:
        raise MetadataError(f"Neočekávaný výstup ExifToolu: {run.stdout[:500]}") from exc
    # ExifTool vrací skupinu XMP-dc, klíče např. "XMP-dc:Identifier"
    return {k: str(v) for k, v in data.items() if k != "SourceFile"}


def write_and_verify(exiftool: str, path: Path, values: dict[str, str]) -> MetadataResult:
    run = write_metadata(exiftool, path, values)
    read_back = read_metadata(exiftool, path, list(values))
    mismatched = [t for t, v in values.items() if read_back.get(t) != v]
    if mismatched:
        details = "; ".join(f"{t}: očekáváno '{values[t]}', přečteno '{read_back.get(t)}'" for t in mismatched)
        raise MetadataError(f"Ověření zapsaných metadat selhalo – {details}")
    return MetadataResult(values=values, run=run, verified=True)
