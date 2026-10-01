"""Orchestrace životního cyklu dávky: kontrola -> metadata -> PREPARED -> síťový archiv.

GUI (gui.py) volá pouze funkce z tohoto modulu, takže celý proces lze testovat i bez GUI.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Callable

import metadata
from config import Config, Preset
from logger import AuditLog, iter_audit_logs, safe_name
from validator import FileCheck, natural_key, scan_batch_folder, validate_file

# Operace běží ve vlákně na pozadí -> zámek zamezí souběhu dvou operací najednou.
_OPERATION_LOCK = threading.Lock()

ProgressFn = Callable[[float, str], None]


class WorkflowError(Exception):
    pass


@dataclass
class BatchResult:
    ok: bool
    status: str
    message: str
    files: list[dict] = field(default_factory=list)
    json_log: Path | None = None
    text_log: Path | None = None
    target_dir: Path | None = None


# --------------------------------------------------------------------------- výpisy


def list_workers(cfg: Config) -> list[str]:
    if not cfg.scans_root.is_dir():
        return []
    return sorted((p.name for p in cfg.scans_root.iterdir() if p.is_dir()), key=natural_key)


def list_batches(cfg: Config, worker: str) -> list[str]:
    folder = cfg.scans_root / worker
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir() if p.is_dir()), reverse=True)


def sha256(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _noop(_fraction: float, _text: str) -> None:
    pass


def _exclusive(operation: Callable[..., BatchResult], *args) -> BatchResult:
    """Spustí operaci, jen pokud zrovna neběží jiná (zpracování i přenos sdílí jeden zámek)."""
    if not _OPERATION_LOCK.acquire(blocking=False):
        return BatchResult(False, "BUSY", "Právě probíhá jiná operace. Počkejte na její dokončení.")
    try:
        return operation(*args)
    finally:
        _OPERATION_LOCK.release()


def _stop(log: AuditLog, stop_note: str, message: str, **details) -> BatchResult:
    """Zaloguje chybu, uzavře audit jako FAILED a vrátí neúspěšný výsledek."""
    log.error(message, **details)
    log.event(stop_note, "ERROR")
    json_path, text_path = log.finish("FAILED", error=message)
    return BatchResult(False, "FAILED", message, log.data["files"], json_path, text_path)


# --------------------------------------------------------------------------- kontrola


def check_batch(cfg: Config, worker: str, batch: str, preset: Preset) -> tuple[list[FileCheck], list[str]]:
    """Kontrola "nanečisto": projde VŠECHNY soubory, nic nemění ani neloguje do auditu.

    Slouží obsluze k nalezení všech chyb najednou před ostrým zpracováním.
    """
    folder = cfg.scans_root / worker / batch
    if not folder.is_dir():
        return [], [f"Složka dávky neexistuje: {folder}"]
    scan = scan_batch_folder(folder, cfg)
    problems = [f"Nepovolená položka ve složce dávky: {p.name}" for p in scan.rejected]
    if not scan.tiffs:
        problems.append("Složka dávky neobsahuje žádné TIFF soubory.")
    return [validate_file(p, preset, cfg) for p in scan.tiffs], problems


# --------------------------------------------------------------------------- zpracování


def process_batch(
    cfg: Config, worker: str, batch: str, preset: Preset, progress: ProgressFn = _noop
) -> BatchResult:
    return _exclusive(_process_batch, cfg, worker, batch, preset, progress)


def _process_batch(cfg: Config, worker: str, batch: str, preset: Preset, progress: ProgressFn) -> BatchResult:
    source = cfg.scans_root / worker / batch
    target = cfg.prepared  # PREPARED je plochý – obsahuje přímo TIFFy
    exiftool = cfg.exiftool_path()
    log = AuditLog(
        cfg.logs,
        f"runner_{safe_name(batch)}_{safe_name(worker)}",
        "batch",
        {
            "title": f"Zpracování dávky {worker} / {batch}",
            "worker": worker,
            "batch": batch,
            "preset_name": preset.name,
            "preset": preset.raw,  # snímek pravidel platných v okamžiku zpracování
            "source_dir": str(source),
            "target_dir": str(target),
            "exiftool_path": exiftool,
            "exiftool_version": metadata.exiftool_version(exiftool),
        },
    )
    log.event(f"Zahájeno zpracování dávky, preset '{preset.name}'.")

    fail = partial(_stop, log, "Dávka ZASTAVENA. Soubory zůstávají ve vstupní složce.")

    # --- 0) předpoklady
    if not source.is_dir():
        return fail(f"Složka dávky neexistuje: {source}")
    if not exiftool or not log.data["exiftool_version"]:
        return fail("ExifTool není dostupný – zkontrolujte tools/exiftool.exe.")
    scan = scan_batch_folder(source, cfg)
    if scan.ignored:
        log.event(f"Ignorované systémové soubory: {', '.join(p.name for p in scan.ignored)}")
    if scan.rejected:
        return fail(
            "Ve složce dávky jsou nepovolené položky (jiné soubory než TIFF nebo podsložky): "
            + ", ".join(p.name for p in scan.rejected)
        )
    if not scan.tiffs:
        return fail("Složka dávky neobsahuje žádné TIFF soubory.")
    clashes = [p.name for p in scan.tiffs if (target / p.name).exists()]
    if clashes:
        return fail(
            "V PREPARED už jsou soubory se stejným názvem (dosud neodeslané do archivu?): " + ", ".join(clashes)
        )
    total = len(scan.tiffs)
    log.event(f"Nalezeno {total} TIFF souborů.")

    # --- 1) validace – první chyba dávku okamžitě zastaví
    checks: list[FileCheck] = []
    for i, path in enumerate(scan.tiffs, 1):
        progress(0.4 * i / total, f"Kontrola {i}/{total}: {path.name}")
        check = validate_file(path, preset, cfg)
        checks.append(check)
        log.add_file(check.to_dict())
        for w in check.warnings:
            log.event(f"{path.name}: {w}", "WARNING")
        if not check.ok:
            return fail(f"{path.name}: {' '.join(check.errors)}", file=path.name)
        log.event(f"{path.name}: kontrola OK ({check.dpi_x:g} DPI, {check.color_mode}, inv. č. {check.inventory}).")
    log.event("Všechny soubory prošly kontrolou.", "OK")

    # --- 2) metadata (SHA-256 před a po zápisu)
    for i, (path, check) in enumerate(zip(scan.tiffs, checks), 1):
        progress(0.4 + 0.5 * i / total, f"Zápis metadat {i}/{total}: {path.name}")
        record = log.data["files"][i - 1]
        try:
            record["sha256_before"] = sha256(path)
            context = {**check.regex_groups, "worker": worker, "batch": batch,
                       "preset": preset.name, "filename": path.name}
            values = metadata.render_values(preset, context)
            result = metadata.write_and_verify(exiftool, path, values)
            record["metadata_written"] = result.values
            record["metadata_verified"] = result.verified
            record["exiftool"] = result.run.to_dict()
            record["sha256_after"] = sha256(path)
        except (metadata.MetadataError, OSError) as exc:
            record["ok"] = False
            record["errors"].append(str(exc))
            return fail(f"{path.name}: zápis metadat selhal – {exc}", file=path.name)
        log.event(f"{path.name}: metadata zapsána a ověřena ({', '.join(values)}).")

    # --- 3) přesun do PREPARED (pouze čisté TIFFy)
    progress(0.95, "Přesun do PREPARED…")
    try:
        target.mkdir(parents=True, exist_ok=True)
        for record, path in zip(log.data["files"], scan.tiffs):
            if (target / path.name).exists():  # souběh – nikdy nepřepisovat
                raise OSError(f"{path.name} už v PREPARED existuje")
            shutil.move(str(path), str(target / path.name))
            record["prepared_path"] = str(target / path.name)
        for junk in scan.ignored:
            junk.unlink(missing_ok=True)
        source.rmdir()
    except OSError as exc:
        return fail(f"Přesun do PREPARED selhal: {exc}. Zkontrolujte ručně obsah {source} a {target}.")
    log.event(f"{total} souborů přesunuto do {target}. Odeslání do archivu spouští obsluha samostatně.", "OK")
    progress(1.0, "Hotovo.")

    json_path, text_path = log.finish("SUCCESS", files=total, target=str(target))
    return BatchResult(True, "SUCCESS", f"Dávka zpracována: {total} souborů přesunuto do PREPARED.",
                       log.data["files"], json_path, text_path, target)


# --------------------------------------------------------------------------- přenos do archivu
#
# Přenos NIKDY neběží automaticky po zpracování – spouští ho obsluha samostatně
# (v GUI dvoukrokově: "Připravit odeslání" -> "Potvrdit a odeslat").

ROBOCOPY_CHUNK = 100  # počet souborů na jedno volání (limit délky příkazové řádky)


@dataclass
class PreparedFile:
    """TIFF v PREPARED spárovaný se svým auditním záznamem zpracování."""

    name: str
    path: Path
    size: int
    worker: str | None = None
    batch: str | None = None
    expected_sha256: str | None = None
    audit_log: str | None = None

    @property
    def audited(self) -> bool:
        return bool(self.expected_sha256)


def audit_index(cfg: Config) -> dict[str, dict]:
    """Název souboru -> poslední úspěšný záznam zpracování (pracovník, dávka, SHA-256 po zápisu)."""
    index: dict[str, dict] = {}
    for path, data in iter_audit_logs(cfg.logs):  # od nejstaršího -> novější přepíše starší
        if data.get("kind") != "batch" or data.get("status") != "SUCCESS":
            continue
        for f in data.get("files", []):
            index[f["filename"]] = {"sha256": f.get("sha256_after"), "worker": data.get("worker"),
                                    "batch": data.get("batch"), "log": str(path)}
    return index


def _prepared_entries(cfg: Config) -> list[Path]:
    """Položky v PREPARED bez systémových souborů (Thumbs.db, skryté soubory…)."""
    if not cfg.prepared.is_dir():
        return []
    return [p for p in cfg.prepared.iterdir()
            if p.name.lower() not in cfg.ignored_files and not p.name.startswith(".")]


def list_prepared(cfg: Config) -> list[PreparedFile]:
    entries = _prepared_entries(cfg)
    if not entries:
        return []
    index = audit_index(cfg)
    out = []
    for p in sorted(entries, key=lambda p: natural_key(p.name)):
        a = index.get(p.name, {})
        out.append(PreparedFile(p.name, p, p.stat().st_size if p.is_file() else 0,
                                a.get("worker"), a.get("batch"), a.get("sha256"), a.get("log")))
    return out


def archive_dir(cfg: Config, worker: str | None, batch: str | None) -> Path:
    sub = cfg.archive_subpath.format(worker=worker or "", batch=batch or "")
    return cfg.archive_root.joinpath(*[p for p in sub.replace("\\", "/").split("/") if p])


def _robocopy(cfg: Config, source: Path, target: Path, files: list[str], move: bool) -> list[dict]:
    """Spustí robocopy (Windows). Na jiném OS použije shutil (kvůli vývoji/testům)."""
    if os.name != "nt" or not shutil.which("robocopy"):
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            if move:
                shutil.move(str(source / name), str(target / name))
            else:
                shutil.copy2(source / name, target / name)
        return [{"command": f"python shutil.{'move' if move else 'copy2'} (robocopy není k dispozici)",
                 "files": files, "returncode": 0, "output": ""}]

    runs = []
    for start in range(0, len(files), ROBOCOPY_CHUNK):
        chunk = files[start:start + ROBOCOPY_CHUNK]
        command = ["robocopy", str(source), str(target), *chunk,
                   "/COPY:DAT", f"/R:{cfg.robocopy_retries}", f"/W:{cfg.robocopy_wait_seconds}",
                   "/NP", "/NJH", "/NDL"]
        if move:
            command.append("/MOV")  # přesun souborů – ekvivalent /MOVE pro vybrané soubory
        proc = subprocess.run(command, capture_output=True, creationflags=metadata.NO_WINDOW)
        output = (proc.stdout + proc.stderr).decode("cp852", errors="replace").strip()
        runs.append({"command": subprocess.list2cmdline(command), "returncode": proc.returncode, "output": output})
        if proc.returncode >= 8:  # robocopy: 0–7 úspěch, >=8 chyba
            raise WorkflowError(f"Robocopy selhal (kód {proc.returncode}): {output[-800:]}")
    return runs


def transfer_files(cfg: Config, names: list[str], progress: ProgressFn = _noop) -> BatchResult:
    return _exclusive(_transfer_files, cfg, names, progress)


def _transfer_files(cfg: Config, names: list[str], progress: ProgressFn) -> BatchResult:
    log = AuditLog(
        cfg.logs,
        f"upload_{datetime.now():%Y-%m-%d}",
        "transfer",
        {"title": "Přenos do síťového archivu", "source_dir": str(cfg.prepared),
         "target_dir": str(cfg.archive_root), "transfer_mode": cfg.transfer_mode},
    )
    log.event(f"Obsluha spustila přenos {len(names)} souborů (režim {cfg.transfer_mode}).")

    fail = partial(_stop, log, "Přenos ZASTAVEN.")

    if not names:
        return fail("Nebyly vybrány žádné soubory.")
    if not cfg.archive_root.is_dir():
        return fail(f"Síťový archiv není dostupný: {cfg.archive_root}. Je disk připojen?")

    prepared = {f.name: f for f in list_prepared(cfg)}
    missing = [n for n in names if n not in prepared]
    if missing:
        return fail(f"Soubory nejsou v PREPARED: {', '.join(missing)}")
    unaudited = [n for n in names if not prepared[n].audited]
    if unaudited:
        return fail(f"K souborům chybí úspěšný auditní záznam zpracování – přenos odmítnut: {', '.join(unaudited)}")

    items = [prepared[n] for n in sorted(names, key=natural_key)]
    total = len(items)
    targets = {f.name: archive_dir(cfg, f.worker, f.batch) / f.name for f in items}
    records = {}
    for f in items:
        records[f.name] = {"filename": f.name, "worker": f.worker, "batch": f.batch,
                           "batch_audit_log": f.audit_log, "ok": False, "target_path": str(targets[f.name])}
        log.add_file(records[f.name])

    # 1) integrita v PREPARED + kolize v archivu (před jakýmkoli kopírováním)
    already = set()
    for i, f in enumerate(items, 1):
        progress(0.3 * i / total, f"Kontrola {i}/{total}: {f.name}")
        rec = records[f.name]
        rec["sha256"] = sha256(f.path)
        if rec["sha256"] != f.expected_sha256:
            rec["errors"] = ["SHA-256 nesouhlasí s auditem"]
            return fail(f"{f.name}: soubor byl po zpracování změněn (SHA-256 nesouhlasí s auditem).")
        target = targets[f.name]
        if target.exists():
            if sha256(target) == f.expected_sha256:
                already.add(f.name)
                log.event(f"{f.name}: v archivu už je identická kopie – kopírování se přeskočí.", "WARNING")
            else:
                rec["errors"] = ["v archivu existuje jiný soubor se stejným názvem"]
                return fail(f"{f.name}: v archivu už existuje JINÝ soubor se stejným názvem. Nic nebylo přeneseno.")
    log.event("Kontrolní součty v PREPARED odpovídají auditu, v archivu nejsou kolize.", "OK")

    # 2) kopírování / přesun (po cílových složkách)
    move = cfg.transfer_mode == "robocopy_move"
    groups: dict[Path, list[str]] = {}
    for f in items:
        if f.name not in already:
            groups.setdefault(targets[f.name].parent, []).append(f.name)
    log.data["robocopy"] = []
    for target_dir, group in groups.items():
        progress(0.35, f"Kopírování {len(group)} souborů do {target_dir}…")
        try:
            log.data["robocopy"] += _robocopy(cfg, cfg.prepared, target_dir, group, move=move)
        except (WorkflowError, OSError) as exc:
            return fail(f"Přenos selhal: {exc}")
        log.event(f"{len(group)} souborů {'přesunuto' if move else 'zkopírováno'} do {target_dir}.")

    # 3) ověření na cíli
    for i, f in enumerate(items, 1):
        progress(0.4 + 0.55 * i / total, f"Ověření v archivu {i}/{total}: {f.name}")
        rec = records[f.name]
        try:
            rec["target_sha256"] = sha256(targets[f.name])
        except OSError as exc:
            return fail(f"{f.name}: nelze přečíst v archivu: {exc}")
        if rec["target_sha256"] != f.expected_sha256:
            rec["errors"] = ["SHA-256 v archivu nesouhlasí"]
            return fail(f"{f.name}: SHA-256 v archivu NESOUHLASÍ! Zdroj {'byl' if move else 'nebyl'} smazán.")
        rec["ok"] = True
    log.event("Všechny soubory v archivu ověřeny (SHA-256).", "OK")

    # 4) úklid PREPARED (až po ověření všech souborů)
    for f in items:
        try:
            f.path.unlink(missing_ok=True)
        except OSError as exc:
            log.event(f"{f.name}: smazání z PREPARED se nezdařilo: {exc}", "WARNING")
    if not _prepared_entries(cfg):  # zůstaly jen Thumbs.db apod.
        for junk in cfg.prepared.iterdir():
            if junk.is_file() and junk.name.lower() in cfg.ignored_files:
                junk.unlink(missing_ok=True)
    progress(1.0, "Hotovo.")

    json_path, text_path = log.finish("SUCCESS", files=total, skipped_already_in_archive=len(already))
    return BatchResult(True, "SUCCESS", f"Přeneseno a ověřeno {total} souborů do {cfg.archive_root}.",
                       log.data["files"], json_path, text_path, cfg.archive_root)
