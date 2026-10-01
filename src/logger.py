"""Auditní logování: strojový JSON + lidsky čitelný .log do app_logs, a system_runner.log.

Logy jsou "věčné" – nikdy se nepřepisují. Pokud soubor se stejným jménem už existuje
(např. opakované zpracování dávky po opravě chyb), přidá se přípona _run2, _run3, ...
"""

from __future__ import annotations

import getpass
import json
import logging
import platform
import re
import socket
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "Inventa"
APP_VERSION = "0.1.0"
SYSTEM_LOG_NAME = "system_runner.log"
LOG_SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_name(text: str) -> str:
    """Název použitelný v názvu souboru (mezery -> -, bez znaků zakázaných ve Windows)."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()
    return re.sub(r"\s+", "-", text) or "nezname"


def setup_system_logger(logs_dir: Path) -> logging.Logger:
    """Logger běhu aplikace (GUI/Python). Bezpečné volat opakovaně (např. při znovunačtení konfigurace)."""
    logger = logging.getLogger("digitalizace")
    target = str((logs_dir / SYSTEM_LOG_NAME).resolve())
    if not any(getattr(h, "baseFilename", None) == target for h in logger.handlers):
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def environment_info() -> dict:
    from PIL import __version__ as pillow_version

    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "python": sys.version.split()[0],
        "pillow": pillow_version,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "os_user": getpass.getuser(),
    }


def iter_audit_logs(logs_dir: Path):
    """Projde auditní JSON logy (od nejstaršího) a vrací dvojice (cesta, data). Poškozené přeskočí."""
    if not logs_dir.is_dir():
        return
    for path in sorted(logs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue


def unique_log_base(logs_dir: Path, base: str) -> Path:
    candidate = logs_dir / base
    n = 1
    while candidate.with_suffix(".json").exists() or candidate.with_suffix(".log").exists():
        n += 1
        candidate = logs_dir / f"{base}_run{n}"
    return candidate


class AuditLog:
    """Sběr událostí jedné operace (zpracování dávky / přenos) a jejich zápis na disk."""

    def __init__(self, logs_dir: Path, base_name: str, kind: str, header: dict):
        self.logs_dir = logs_dir
        self.base_name = base_name
        self.kind = kind
        self.data: dict = {
            "schema_version": LOG_SCHEMA_VERSION,
            "kind": kind,
            "status": "RUNNING",
            "started_at": now_iso(),
            "finished_at": None,
            **header,
            "environment": environment_info(),
            "files": [],
            "events": [],
            "errors": [],
        }
        self.json_path: Path | None = None
        self.log_path: Path | None = None
        self._sys = logging.getLogger("digitalizace")

    def event(self, message: str, level: str = "INFO", **details) -> None:
        entry = {"time": now_iso(), "level": level, "message": message}
        if details:
            entry["details"] = details
        self.data["events"].append(entry)
        self._sys.log(logging.getLevelName(level) if level != "OK" else logging.INFO, "[%s] %s", self.kind, message)

    def error(self, message: str, **details) -> None:
        self.data["errors"].append({"time": now_iso(), "message": message, **details})
        self.event(message, "ERROR", **details)

    def add_file(self, record: dict) -> None:
        self.data["files"].append(record)

    def finish(self, status: str, **summary) -> tuple[Path, Path]:
        self.data["status"] = status
        self.data["finished_at"] = now_iso()
        self.data["summary"] = summary
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        base = unique_log_base(self.logs_dir, self.base_name)
        self.json_path, self.log_path = base.with_suffix(".json"), base.with_suffix(".log")
        # 'x' = exkluzivní vytvoření -> nikdy nepřepíšeme existující audit
        with open(self.json_path, "x", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        with open(self.log_path, "x", encoding="utf-8") as fh:
            fh.write(self.render_text())
        return self.json_path, self.log_path

    def render_text(self) -> str:
        d = self.data
        line = "=" * 78
        out = [
            line,
            f" {d.get('title', self.kind)}",
            line,
            f"Stav:           {d['status']}",
            f"Zahájeno:       {d['started_at']}",
            f"Dokončeno:      {d['finished_at']}",
        ]
        for key, label in (
            ("worker", "Pracovník:"),
            ("batch", "Dávka:"),
            ("preset_name", "Preset:"),
            ("source_dir", "Zdroj:"),
            ("target_dir", "Cíl:"),
        ):
            if d.get(key):
                out.append(f"{label:<16}{d[key]}")
        env = d["environment"]
        out.append(f"Obsluha (OS):   {env['os_user']} @ {env['hostname']}")
        if d.get("exiftool_version"):
            out.append(f"ExifTool:       {d['exiftool_version']}")
        out += ["", "ČASOVÁ OSA", "-" * 78]
        for e in d["events"]:
            out.append(f"{e['time']}  {e['level']:<5}  {e['message']}")
        if d["files"]:
            out += ["", "SOUBORY", "-" * 78]
            for f in d["files"]:
                out.append(f"{'OK ' if f.get('ok') else 'CHYBA'}  {f.get('filename')}")
                for key, label in (
                    ("inventory", "inv. číslo"),
                    ("sha256_before", "SHA-256 před"),
                    ("sha256_after", "SHA-256 po"),
                    ("sha256", "SHA-256"),
                    ("batch", "dávka"),
                    ("target_path", "cíl"),
                    ("target_sha256", "SHA-256 v cíli"),
                ):
                    if f.get(key):
                        out.append(f"       {label}: {f[key]}")
                if f.get("dpi_x") is not None:
                    out.append(
                        f"       {f['width']}×{f['height']} px, {f['dpi_x']:g}×{f['dpi_y']:g} DPI, "
                        f"{f.get('color_mode')} {f.get('bits_per_sample')}, komprese {f.get('compression')}"
                    )
                for err in f.get("errors", []):
                    out.append(f"       ! {err}")
                for w in f.get("warnings", []):
                    out.append(f"       ? {w}")
        if d["errors"]:
            out += ["", "CHYBY", "-" * 78]
            out += [f"{e['time']}  {e['message']}" for e in d["errors"]]
        if d.get("summary"):
            out += ["", "SOUHRN", "-" * 78]
            out += [f"{k}: {v}" for k, v in d["summary"].items()]
        out.append("")
        return "\n".join(out)
