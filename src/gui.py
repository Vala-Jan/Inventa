"""Desktopové rozhraní (Tkinter) – běžné okno Windows, žádný server ani prohlížeč.

Spouští se přes Inventa.bat (pythonw.exe -> bez konzolového okna).
Dlouhé operace běží ve vlákně na pozadí, okno mezitím reaguje a ukazuje průběh.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent))

import metadata  # noqa: E402
import workflow  # noqa: E402
from config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config, save_user_paths  # noqa: E402
from logger import APP_NAME, APP_VERSION, iter_audit_logs, setup_system_logger  # noqa: E402

APP_TAGLINE = "validation, metadata & ingest pipeline for museum digitization"
KIND_LABELS = {"batch": "runner", "transfer": "upload"}

BANNER_STYLES = {
    "error": ("#fde2e2", "#9b1c1c", "✖"),
    "ok": ("#dcf5e3", "#14532d", "✔"),
    "warn": ("#fff4cc", "#713f12", "⚠"),
    "info": ("#e8f0fe", "#1e3a8a", "ℹ"),
}
GREEN, RED, GREY = "#15803d", "#b91c1c", "#4b5563"
SIDE_BG = "#f1f3f6"


def enable_high_dpi() -> None:
    """Bez tohoto je okno na monitorech se zvětšením 125 % a více rozmazané."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):  # starší Windows bez shcore
        pass


def is_dir(path: Path | None) -> bool:
    return path is not None and path.is_dir()


def path_text(path: Path | None) -> str:
    return str(path) if path else "nenastaveno – klikněte na „Nastavit cesty…“"


def open_in_system(path: Path) -> None:
    """Otevře soubor/složku v příslušném programu (Průzkumník, Poznámkový blok…)."""
    os.startfile(path)


# --------------------------------------------------------------------------- pomocné widgety


def auto_wrap(label, margin: int = 10):
    """Zalamuje text popisku podle jeho aktuální šířky."""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(200, e.width - margin)))
    return label


class Banner(tk.Frame):
    """Pevné místo v okně pro barevnou zprávu (červená = chyba, zelená = OK, …).

    Rámeček zůstává stále zabalený (prázdný má nulovou výšku), zpráva se jen zobrazuje/skrývá,
    takže se nemění pořadí prvků v okně.
    """

    def __init__(self, master, **pack):
        super().__init__(master)
        self.label = auto_wrap(tk.Label(self, anchor="w", justify="left", padx=12, pady=8), margin=30)
        self.pack(fill="x", **pack)

    def show(self, kind: str, text: str) -> None:
        bg, fg, icon = BANNER_STYLES[kind]
        self.label.configure(text=f"{icon}  {text}", bg=bg, fg=fg)
        self.label.pack(fill="x", pady=(0, 8))

    def hide(self) -> None:
        self.label.pack_forget()
        self.configure(height=1)  # Tk jinak ponechá rámeček v původní výšce


def wrapping_label(master, **kw) -> ttk.Label:
    """Popisek, který zalamuje text podle aktuální šířky okna."""
    return auto_wrap(ttk.Label(master, justify="left", **kw))


class RunBox(ttk.Frame):
    """Pevné místo pro ukazatel průběhu operace."""

    def __init__(self, master):
        super().__init__(master)
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1.0)
        self.status = ttk.Label(self, foreground="#374151")
        self.pack(fill="x")

    def start(self) -> None:
        self.bar["value"] = 0
        self.status.configure(text="Zahajuji…")
        self.bar.pack(fill="x", pady=(4, 2))
        self.status.pack(fill="x", pady=(0, 8))

    def set_progress(self, fraction: float, text: str) -> None:
        self.bar["value"] = fraction
        self.status.configure(text=text)

    def stop(self) -> None:
        self.bar.pack_forget()
        self.status.pack_forget()
        self.configure(height=1)  # Tk jinak ponechá rámeček v původní výšce


class Table(ttk.Frame):
    """Treeview se svislým i vodorovným posuvníkem."""

    def __init__(self, master, columns: list[tuple[str, str, int]], height: int = 10, selectmode="browse",
                 sort: tuple[str, bool] | None = None):
        super().__init__(master)
        self.keys = [c[0] for c in columns]
        self.tree = ttk.Treeview(self, columns=self.keys, show="headings", height=height, selectmode=selectmode)
        self.labels = {key: label for key, label, _ in columns}
        self._sort = sort  # (sloupec, sestupně)
        for key, label, width in columns:
            self.tree.heading(key, text=label, anchor="w", command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, minwidth=40, anchor="w", stretch=key == self.keys[-1])
        ys = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.tag_configure("bad", foreground=RED)
        self.tree.tag_configure("good", foreground="#14532d")

    def set_rows(self, rows: list[dict], iid_key: str | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(rows):
            tag = row.get("_tag", "")
            self.tree.insert("", "end", iid=str(row[iid_key]) if iid_key else str(i),
                             values=[row.get(k, "") for k in self.keys], tags=(tag,) if tag else ())
        if self._sort:
            self._apply_sort()

    def sort_by(self, key: str) -> None:
        """Klik na záhlaví: řazení vzestupně, další klik sestupně."""
        reverse = bool(self._sort and self._sort[0] == key and not self._sort[1])
        self._sort = (key, reverse)
        self._apply_sort()

    def _apply_sort(self) -> None:
        key, reverse = self._sort

        def sort_key(iid):
            value = self.tree.set(iid, key)
            try:
                return (0, float(value), "")
            except ValueError:
                return (1, 0.0, value.lower())

        for index, iid in enumerate(sorted(self.tree.get_children(), key=sort_key, reverse=reverse)):
            self.tree.move(iid, "", index)
        for k in self.keys:
            arrow = (" ▼" if reverse else " ▲") if k == key else ""
            self.tree.heading(k, text=self.labels[k] + arrow)


def file_rows(records: list[dict]) -> list[dict]:
    rows = []
    for r in records:
        problems = r.get("errors", []) + [f"⚠ {w}" for w in r.get("warnings", [])]
        rows.append({
            "_tag": "good" if r.get("ok") else "bad",
            "stav": "OK" if r.get("ok") else "CHYBA",
            "soubor": r.get("filename"),
            "inv": r.get("inventory") or "–",
            "dpi": f"{r['dpi_x']:g}×{r['dpi_y']:g}" if r.get("dpi_x") is not None else "–",
            "rezim": r.get("color_mode") or "–",
            "bity": ",".join(map(str, r.get("bits_per_sample") or [])) or "–",
            "komprese": r.get("compression") or "–",
            "rozmer": f"{r['width']}×{r['height']}" if r.get("width") else "–",
            "chyby": " | ".join(problems),
        })
    return rows


FILE_COLUMNS = [
    ("stav", "Stav", 60), ("soubor", "Soubor", 210), ("inv", "Inv. číslo", 90), ("dpi", "DPI", 80),
    ("rezim", "Režim", 80), ("bity", "Bity", 60), ("komprese", "Komprese", 80), ("rozmer", "Rozměr", 90),
    ("chyby", "Chyby / upozornění", 400),
]


PATH_FIELDS = [
    ("scans_root", "Vstupní složka se skeny", "dir",
     "složka, ve které má každý pracovník svou podsložku s dávkami (…\\Pracovníci)"),
    ("archive_root", "Síťový archiv", "dir", "kam se odesílají hotové soubory, např. Z:\\Digitalizace"),
    ("prepared", "PREPARED", "dir", "mezisklad zpracovaných TIFFů; relativní cesta = uvnitř složky aplikace"),
    ("logs", "Logy", "dir", "auditní logy; relativní cesta = uvnitř složky aplikace"),
    ("exiftool", "ExifTool", "file", "exiftool.exe; když soubor neexistuje, použije se exiftool z PATH"),
]


class PathsDialog(tk.Toplevel):
    """Výběr cest uživatelem. Uloží se do config/settings.json (presets.json se nemění)."""

    def __init__(self, master: "App", cfg: Config, on_save):
        super().__init__(master)
        self.title("Nastavení cest")
        self.transient(master)
        self.resizable(True, False)
        self.on_save = on_save
        self.app_root = cfg.app_root
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        self.vars: dict[str, tk.StringVar] = {}
        for i, (key, label, kind, hint) in enumerate(PATH_FIELDS):
            row = 2 * i
            self.vars[key] = tk.StringVar(value=cfg.paths_raw.get(key, ""))
            ttk.Label(body, text=label, font=master.font_bold).grid(row=row, column=0, sticky="w",
                                                                    padx=(0, 10), pady=(8, 0))
            ttk.Entry(body, textvariable=self.vars[key], width=70).grid(row=row, column=1, sticky="ew", pady=(8, 0))
            ttk.Button(body, text="Procházet…", command=lambda k=key, t=kind, l=label: self._browse(k, t, l)).grid(
                row=row, column=2, padx=(8, 0), pady=(8, 0))
            ttk.Label(body, text=hint, foreground=GREY, font=master.font_small).grid(row=row + 1, column=1, sticky="w")
        bar = ttk.Frame(body)
        bar.grid(row=2 * len(PATH_FIELDS), column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(bar, text="Uložit", style="Accent.TButton", command=self._save).pack(side="left")
        ttk.Button(bar, text="Zrušit", command=self.destroy).pack(side="left", padx=(8, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _browse(self, key: str, kind: str, title: str) -> None:
        current = self.vars[key].get().strip()
        start = Path(current) if current else self.app_root
        if not start.is_absolute():
            start = self.app_root / start
        folder = start if kind == "dir" else start.parent
        options = {"parent": self, "title": title}
        if folder.is_dir():
            options["initialdir"] = str(folder)
        if kind == "dir":
            chosen = filedialog.askdirectory(mustexist=True, **options)
        else:
            chosen = filedialog.askopenfilename(filetypes=[("Programy", "*.exe"), ("Všechny soubory", "*.*")],
                                                **options)
        if chosen:
            self.vars[key].set(chosen)

    def _save(self) -> None:
        paths = {key: var.get().strip() for key, var in self.vars.items()}
        self.destroy()
        self.on_save(paths)


# --------------------------------------------------------------------------- hlavní okno


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1280x820")
        self.minsize(1000, 640)
        self._setup_style()

        self.cfg: Config | None = None
        self.exiftool_ver: str | None = None
        self.exiftool_checked = False
        self.busy = False
        self.events: queue.Queue = queue.Queue()
        self.syslog = None
        self.pending_transfer: list[str] | None = None
        self.transfer_groups: dict[str, list[workflow.PreparedFile]] = {}
        self.last_logs: dict[str, Path | None] = {"process": None, "transfer": None}
        self._current_run: RunBox | None = None
        self._paths_prompted = False  # okno Nastavení cest se samo otevře jen jednou

        self.report_callback_exception = self._on_tk_exception
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self.reload()
        self.after(100, self._poll_events)

    # ------------------------------------------------------------------ vzhled

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=10)
        tkfont.nametofont("TkTextFont").configure(size=10)
        self.font_bold = base.copy()
        self.font_bold.configure(weight="bold")
        self.font_title = base.copy()
        self.font_title.configure(size=16, weight="bold")
        self.font_small = base.copy()
        self.font_small.configure(size=9)
        style.configure("Treeview", rowheight=int(base.metrics("linespace") * 1.6))
        style.configure("Treeview.Heading", font=self.font_bold)
        style.configure("TButton", padding=(10, 5))
        style.configure("Accent.TButton", font=self.font_bold, padding=(14, 6))
        style.configure("Side.TFrame", background=SIDE_BG)
        style.configure("Side.TLabel", background=SIDE_BG)

    def _build(self) -> None:
        # postranní panel se stavem systému
        side = ttk.Frame(self, style="Side.TFrame", padding=12, width=280)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        ttk.Label(side, text="Stav systému", font=self.font_bold, style="Side.TLabel").pack(anchor="w", pady=(0, 8))
        self.status_box = ttk.Frame(side, style="Side.TFrame")
        self.status_box.pack(fill="x")
        ttk.Button(side, text="⟳  Obnovit / načíst konfiguraci", command=self.reload).pack(fill="x", pady=(14, 4))
        ttk.Button(side, text="Nastavit cesty…", command=self.open_paths_dialog).pack(fill="x", pady=4)
        ttk.Button(side, text="Otevřít složku logů",
                   command=lambda: self._open(self.cfg.logs if self.cfg else None)).pack(fill="x", pady=4)
        ttk.Button(side, text="Otevřít PREPARED",
                   command=lambda: self._open(self.cfg.prepared if self.cfg else None)).pack(fill="x", pady=4)
        ttk.Label(side, text=f"{APP_NAME}  v{APP_VERSION}", font=self.font_small,
                  style="Side.TLabel", foreground="#6b7280").pack(side="bottom", anchor="w")

        main = ttk.Frame(self, padding=(16, 10, 16, 10))
        main.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text=APP_NAME, font=self.font_title).pack(side="left")
        ttk.Label(header, text=f"  –  {APP_TAGLINE}", foreground=GREY).pack(side="left", pady=(6, 0))
        self.config_banner = Banner(main)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True)
        self.tab_process = ttk.Frame(self.notebook, padding=12)
        self.tab_transfer = ttk.Frame(self.notebook, padding=12)
        self.tab_history = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.tab_process, text="  1  Zpracování dávky  ")
        self.notebook.add(self.tab_transfer, text="  2  Odeslání do archivu  ")
        self.notebook.add(self.tab_history, text="  Historie a logy  ")
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._on_tab_changed())

        self._build_process_tab()
        self._build_transfer_tab()
        self._build_history_tab()

    # ------------------------------------------------------------------ záložka 1

    def _build_process_tab(self) -> None:
        t = self.tab_process
        form = ttk.Frame(t)
        form.pack(fill="x")
        for col, label in enumerate(("Pracovník", "Dávka (složka)", "Preset")):
            ttk.Label(form, text=label).grid(row=0, column=col, sticky="w", padx=(0, 12))
            form.columnconfigure(col, weight=1, uniform="f")
        self.cb_worker = ttk.Combobox(form, state="readonly")
        self.cb_batch = ttk.Combobox(form, state="readonly")
        self.cb_preset = ttk.Combobox(form, state="readonly")
        for col, cb in enumerate((self.cb_worker, self.cb_batch, self.cb_preset)):
            cb.grid(row=1, column=col, sticky="ew", padx=(0, 12), pady=(2, 8))
        self.cb_worker.bind("<<ComboboxSelected>>", lambda e: self._refresh_batches())
        self.cb_batch.bind("<<ComboboxSelected>>", lambda e: self._on_selection_changed())
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self._on_selection_changed())

        self.preset_info = ttk.Label(t, foreground="#374151", justify="left")
        self.preset_info.pack(fill="x", pady=(0, 4))
        self.folder_info = wrapping_label(t, foreground="#6b7280", font=self.font_small)
        self.folder_info.pack(fill="x", pady=(0, 8))

        buttons = ttk.Frame(t)
        buttons.pack(fill="x", pady=(0, 4))
        self.btn_check = ttk.Button(buttons, text="Zkontrolovat (bez zápisu)", command=self.on_check)
        self.btn_check.pack(side="left")
        self.btn_process = ttk.Button(buttons, text="▶  Zpracovat dávku", style="Accent.TButton",
                                      command=self.on_process)
        self.btn_process.pack(side="left", padx=10)
        self.btn_process_log = ttk.Button(buttons, text="Otevřít log dávky", state="disabled",
                                          command=lambda: self._open(self.last_logs["process"]))
        self.btn_process_log.pack(side="right")

        self.process_run = RunBox(t)
        self.process_banner = Banner(t, pady=(4, 0))
        self.process_table = Table(t, FILE_COLUMNS, height=12)
        self.process_table.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ záložka 2

    def _build_transfer_tab(self) -> None:
        t = self.tab_transfer
        self.transfer_target = wrapping_label(t, font=self.font_bold)
        self.transfer_target.pack(fill="x")
        wrapping_label(t, foreground=GREY, text=(
            "Přenos se nikdy nespouští automaticky po zpracování. Obsluha ho zahájí ve dvou krocích: "
            "„Připravit odeslání“ → kontrola souhrnu → „Potvrdit a odeslat“."
        )).pack(fill="x", pady=(2, 8))
        self.unaudited_banner = Banner(t)
        self.transfer_banner = Banner(t)

        ttk.Label(t, text="Dávky v PREPARED (výběr více dávek: Ctrl / Shift + klik):").pack(anchor="w")
        self.transfer_table = Table(t, [
            ("davka", "Dávka", 260), ("pocet", "Souborů", 80), ("velikost", "Velikost (MB)", 110),
            ("cil", "Cíl", 500),
        ], height=8, selectmode="extended")
        self.transfer_table.pack(fill="both", expand=True, pady=(2, 8))
        self.transfer_table.tree.bind("<<TreeviewSelect>>", lambda e: self._cancel_pending_transfer())

        bar = ttk.Frame(t)
        bar.pack(fill="x")
        self.btn_prepare = ttk.Button(bar, text="Připravit odeslání  ›", command=self.on_prepare_transfer)
        self.btn_prepare.pack(side="left")
        self.btn_transfer_log = ttk.Button(bar, text="Otevřít log přenosu", state="disabled",
                                           command=lambda: self._open(self.last_logs["transfer"]))
        self.btn_transfer_log.pack(side="right")

        # krok 2 – zobrazí se až po "Připravit odeslání"
        slot = ttk.Frame(t)
        slot.pack(fill="x")
        warn_bg, warn_fg, _ = BANNER_STYLES["warn"]
        self.confirm_frame = tk.Frame(slot, bg=warn_bg, padx=12, pady=10)
        self.confirm_text = auto_wrap(tk.Label(self.confirm_frame, bg=warn_bg, fg=warn_fg, justify="left",
                                               anchor="w", font=self.font_bold))
        self.confirm_text.pack(fill="x")
        cbar = tk.Frame(self.confirm_frame, bg=warn_bg)
        cbar.pack(fill="x", pady=(8, 0))
        self.btn_confirm = ttk.Button(cbar, text="✔  Potvrdit a odeslat", style="Accent.TButton",
                                      command=self.on_confirm_transfer)
        self.btn_confirm.pack(side="left")
        ttk.Button(cbar, text="✖  Zrušit", command=self._cancel_pending_transfer).pack(side="left", padx=10)

        self.transfer_run = RunBox(t)

    # ------------------------------------------------------------------ záložka 3

    def _build_history_tab(self) -> None:
        t = self.tab_history
        self.history_entries: list[dict] = []

        # --- filtry a vyhledávání
        filters = ttk.Frame(t)
        filters.pack(fill="x", pady=(0, 6))
        self.hist_search = tk.StringVar()
        self.hist_kind = tk.StringVar(value="Vše")
        self.hist_status = tk.StringVar(value="Vše")
        self.hist_worker = tk.StringVar(value="Vše")
        self.hist_from = tk.StringVar()
        self.hist_to = tk.StringVar()

        top_row = ttk.Frame(filters)
        top_row.pack(fill="x")
        ttk.Label(top_row, text="Hledat:").pack(side="left")
        search = ttk.Entry(top_row, textvariable=self.hist_search)
        search.pack(side="left", fill="x", expand=True, padx=(6, 10))
        ttk.Button(top_row, text="Zrušit filtry", command=self._reset_history_filters).pack(side="left")
        ttk.Label(filters, foreground=GREY, font=self.font_small, text=(
            "Hledá se v názvu logu, pracovníkovi, dávce, presetu, názvech souborů, inventárních číslech a chybách. "
            "Esc vymaže hledání."
        )).pack(anchor="w", pady=(2, 6))

        row = ttk.Frame(filters)
        row.pack(fill="x")
        for label, var, values, width in (
            ("Typ:", self.hist_kind, ["Vše", "runner", "upload"], 9),
            ("Stav:", self.hist_status, ["Vše", "SUCCESS", "FAILED"], 10),
            ("Pracovník:", self.hist_worker, ["Vše"], 18),
        ):
            ttk.Label(row, text=label).pack(side="left")
            cb = ttk.Combobox(row, textvariable=var, values=values, state="readonly", width=width)
            cb.pack(side="left", padx=(4, 14))
            if var is self.hist_worker:
                self.hist_worker_cb = cb
        ttk.Label(row, text="Datum od:").pack(side="left")
        ttk.Entry(row, textvariable=self.hist_from, width=12).pack(side="left", padx=(4, 6))
        ttk.Label(row, text="do:").pack(side="left")
        ttk.Entry(row, textvariable=self.hist_to, width=12).pack(side="left", padx=(4, 6))
        ttk.Label(row, text="(RRRR-MM-DD)", foreground=GREY, font=self.font_small).pack(side="left")

        self._hist_after: str | None = None
        for var in (self.hist_search, self.hist_kind, self.hist_status, self.hist_worker, self.hist_from,
                    self.hist_to):
            var.trace_add("write", lambda *_: self._schedule_history_filter())

        # --- tabulka + detail
        pane = ttk.PanedWindow(t, orient="vertical")
        pane.pack(fill="both", expand=True)
        top = ttk.Frame(pane)
        self.history_count = ttk.Label(top, foreground=GREY)
        self.history_count.pack(anchor="w", pady=(0, 2))
        self.history_table = Table(top, [
            ("datum", "Datum", 140), ("typ", "Typ", 70), ("stav", "Stav", 80), ("pracovnik", "Pracovník", 170),
            ("davka", "Dávka", 100), ("preset", "Preset", 160), ("souboru", "Souborů", 70),
            ("log", "Log", 260), ("chyba", "Chyba", 400),
        ], height=8, sort=("datum", True))  # výchozí: nejnovější nahoře
        self.history_table.pack(fill="both", expand=True)

        bottom = ttk.Frame(pane)
        bar = ttk.Frame(bottom)
        bar.pack(fill="x", pady=(6, 4))
        self.btn_hist_log = ttk.Button(bar, text="Otevřít .log", state="disabled",
                                       command=lambda: self._open_history_file(".log"))
        self.btn_hist_log.pack(side="left")
        self.btn_hist_json = ttk.Button(bar, text="Otevřít .json", state="disabled",
                                        command=lambda: self._open_history_file(".json"))
        self.btn_hist_json.pack(side="left", padx=8)
        self.history_detail = ttk.Label(bar, foreground=GREY)
        self.history_detail.pack(side="left", padx=8)
        self.history_text = ScrolledText(bottom, wrap="none", font=("Consolas", 10), height=14)
        self.history_text.tag_configure("hit", background="#fde68a")
        self.history_text.tag_configure("error", foreground=RED)
        self.history_text.configure(state="disabled")
        self.history_text.pack(fill="both", expand=True)

        pane.add(top, weight=1)
        pane.add(bottom, weight=2)
        self.history_table.tree.bind("<<TreeviewSelect>>", lambda e: self._show_history_log())
        self.history_table.tree.bind("<Double-1>", lambda e: self._open_history_file(".log"))
        search.bind("<Escape>", lambda e: self.hist_search.set(""))

    # ------------------------------------------------------------------ načtení / obnovení

    def reload(self) -> None:
        if self.busy:
            return
        try:
            self.cfg = load_config(DEFAULT_CONFIG_PATH)
        except ConfigError as exc:
            self.cfg = None
            self.config_banner.show("error", f"Chyba konfigurace ({DEFAULT_CONFIG_PATH}): {exc}")
            self._render_status()
            self._update_buttons()
            messagebox.showerror("Chyba konfigurace", str(exc))
            return
        self.config_banner.hide()
        self.syslog = setup_system_logger(self.cfg.logs)
        self.exiftool_ver, self.exiftool_checked = None, False
        threading.Thread(target=self._detect_exiftool, args=(self.cfg.exiftool_path(),), daemon=True).start()

        self.cb_preset["values"] = list(self.cfg.presets)
        if self.cb_preset.get() not in self.cfg.presets:
            self.cb_preset.set(next(iter(self.cfg.presets)))
        self._refresh_workers()
        self._refresh_transfer()
        self._refresh_history()
        self._render_status()
        if self.cfg.missing_paths():
            self.config_banner.show("info", "Nastavte vstupní složku se skeny a síťový archiv – "
                                            "tlačítko „Nastavit cesty…“ v levém panelu.")
            if not self._paths_prompted:
                self._paths_prompted = True
                self.after(300, self.open_paths_dialog)

    def _detect_exiftool(self, path: str | None) -> None:
        self.events.put(("exiftool", metadata.exiftool_version(path), None))

    def _render_status(self) -> None:
        for w in self.status_box.winfo_children():
            w.destroy()
        cfg = self.cfg
        if cfg is None:
            ttk.Label(self.status_box, text="Konfigurace nenačtena", foreground=RED,
                      style="Side.TLabel").pack(anchor="w")
            return
        if self.exiftool_ver:
            exif_state = self.exiftool_ver
        else:
            exif_state = "NENALEZEN" if self.exiftool_checked else "zjišťuji…"
        items = [
            (bool(self.exiftool_ver), "ExifTool",
             f"{exif_state} – {cfg.exiftool_path() or cfg.exiftool_configured}"),
            (is_dir(cfg.scans_root), "Vstup (Pracovníci)", path_text(cfg.scans_root)),
            (cfg.prepared.is_dir(), "PREPARED", str(cfg.prepared)),
            (is_dir(cfg.archive_root), "Síťový archiv", path_text(cfg.archive_root)),
            (cfg.logs.is_dir(), "Logy", str(cfg.logs)),
        ]
        for ok, label, detail in items:
            row = ttk.Frame(self.status_box, style="Side.TFrame")
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text=f"● {label}", foreground=GREEN if ok else RED, font=self.font_bold,
                      style="Side.TLabel").pack(anchor="w")
            ttk.Label(row, text=detail, font=self.font_small, wraplength=250, foreground=GREY,
                      style="Side.TLabel").pack(anchor="w")

    def _refresh_workers(self) -> None:
        workers = workflow.list_workers(self.cfg) if self.cfg else []
        self.cb_worker["values"] = workers
        if self.cb_worker.get() not in workers:
            self.cb_worker.set(workers[0] if workers else "")
        self._refresh_batches()

    def _refresh_batches(self) -> None:
        worker = self.cb_worker.get()
        batches = workflow.list_batches(self.cfg, worker) if self.cfg and worker else []
        self.cb_batch["values"] = batches
        if self.cb_batch.get() not in batches:
            self.cb_batch.set(batches[0] if batches else "")
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        cfg = self.cfg
        preset = cfg.presets.get(self.cb_preset.get()) if cfg else None
        if preset:
            meta = ", ".join(f"{t} = {v}" for t, v in preset.metadata.items())
            self.preset_info.configure(text=(
                f"{preset.description}\n"
                f"Min. DPI: {preset.min_dpi:g}   ·   Režimy: {', '.join(preset.allowed_color_modes)}   ·   "
                f"Bitová hloubka: {preset.allowed_bit_depths or 'libovolná'}   ·   "
                f"Komprese: {preset.allowed_compressions or 'libovolná'}\n"
                f"Metadata: {meta}"
            ))
        else:
            self.preset_info.configure(text="")
        worker, batch = self.cb_worker.get(), self.cb_batch.get()
        if cfg is None:
            self.folder_info.configure(text="")
        elif cfg.scans_root is None:
            self.folder_info.configure(text=workflow.SCANS_NOT_SET)
        elif not worker:
            self.folder_info.configure(text=f"Ve vstupní složce nejsou žádní pracovníci: {cfg.scans_root}")
        elif not batch:
            self.folder_info.configure(text="Pracovník nemá žádnou dávku ke zpracování.")
        else:
            folder = cfg.scans_root / worker / batch
            count = sum(1 for p in folder.iterdir() if p.is_file()) if folder.is_dir() else 0
            self.folder_info.configure(text=f"Složka: {folder}   ({count} souborů)")
        self._update_buttons()

    def _refresh_transfer(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        self.transfer_target.configure(text=f"Cíl: {path_text(cfg.archive_root)}    ·    "
                                            f"režim přenosu: {cfg.transfer_mode}")
        prepared = workflow.list_prepared(cfg)
        self.transfer_groups = {}
        for f in prepared:
            if f.audited:
                self.transfer_groups.setdefault(f"{f.worker} / {f.batch}", []).append(f)
        unaudited = [f.name for f in prepared if not f.audited]
        if unaudited:
            self.unaudited_banner.show("error", "Tyto soubory v PREPARED nemají úspěšný auditní záznam zpracování "
                                                "a nelze je odeslat: " + ", ".join(unaudited))
        else:
            self.unaudited_banner.hide()
        rows = [
            {"davka": label, "pocet": len(files), "velikost": f"{sum(f.size for f in files) / 1e6:.1f}",
             "cil": str(workflow.archive_dir(cfg, files[0].worker, files[0].batch)) if cfg.archive_root else "–"}
            for label, files in self.transfer_groups.items()
        ]
        self.transfer_table.set_rows(rows, iid_key="davka")
        self.transfer_table.tree.selection_set(list(self.transfer_groups))  # výchozí: vše
        self._cancel_pending_transfer()
        if cfg.archive_root is None:
            self.transfer_banner.show("error", workflow.ARCHIVE_NOT_SET)
        elif not cfg.archive_root.is_dir():
            self.transfer_banner.show("error", f"Síťový archiv {cfg.archive_root} není dostupný. "
                                               "Připojte síťový disk.")
        elif not rows:
            self.transfer_banner.show("info", "V PREPARED nejsou žádné zpracované soubory k odeslání.")
        else:
            self.transfer_banner.hide()

    def _refresh_history(self) -> None:
        """Načte všechny auditní JSON logy (filtrování pak probíhá jen v paměti)."""
        cfg = self.cfg
        entries = []
        if cfg:
            for p, d in iter_audit_logs(cfg.logs):
                if d.get("kind") not in KIND_LABELS:
                    continue
                files = d.get("files", [])
                errors = [e.get("message", "") for e in d.get("errors", [])]
                started = str(d.get("started_at") or "")
                haystack = " ".join([
                    p.stem, str(d.get("worker") or ""), str(d.get("batch") or ""), str(d.get("preset_name") or ""),
                    *errors,
                    *(f"{f.get('filename', '')} {f.get('inventory') or ''} {f.get('batch') or ''} "
                      f"{f.get('worker') or ''}" for f in files),
                ]).lower()
                entries.append({
                    "path": p, "haystack": haystack, "date": started[:10],
                    "row": {
                        "_tag": "good" if d.get("status") == "SUCCESS" else "bad",
                        "datum": started[:16].replace("T", " "),
                        "typ": KIND_LABELS[d["kind"]], "stav": d.get("status") or "–",
                        "pracovnik": d.get("worker") or "–", "davka": d.get("batch") or "–",
                        "preset": d.get("preset_name") or "–", "souboru": len(files),
                        "log": p.stem, "chyba": errors[0] if errors else "",
                    },
                })
        self.history_entries = entries
        workers = sorted({e["row"]["pracovnik"] for e in entries if e["row"]["pracovnik"] != "–"})
        self.hist_worker_cb["values"] = ["Vše", *workers]
        if self.hist_worker.get() not in self.hist_worker_cb["values"]:
            self.hist_worker.set("Vše")
        self._apply_history_filter()

    def _schedule_history_filter(self) -> None:
        """Filtr se aplikuje chvíli po posledním stisku klávesy (plynulé psaní)."""
        if self._hist_after:
            self.after_cancel(self._hist_after)
        self._hist_after = self.after(200, self._apply_history_filter)

    def _apply_history_filter(self) -> None:
        self._hist_after = None
        terms = self.hist_search.get().lower().split()
        kind, status, worker = self.hist_kind.get(), self.hist_status.get(), self.hist_worker.get()
        date_from, date_to = self.hist_from.get().strip(), self.hist_to.get().strip()
        shown = []
        for e in self.history_entries:
            row = e["row"]
            if kind != "Vše" and row["typ"] != kind:
                continue
            if status != "Vše" and row["stav"] != status:
                continue
            if worker != "Vše" and row["pracovnik"] != worker:
                continue
            if date_from and e["date"] < date_from:
                continue
            if date_to and e["date"] > date_to:
                continue
            if any(term not in e["haystack"] for term in terms):
                continue
            shown.append(row)
        selected = self.history_table.tree.selection()
        self.history_table.set_rows(shown, iid_key="log")
        self.history_count.configure(text=f"Zobrazeno {len(shown)} z {len(self.history_entries)} logů")
        if selected and self.history_table.tree.exists(selected[0]):
            self.history_table.tree.selection_set(selected[0])
        else:
            self._show_history_log()

    def _reset_history_filters(self) -> None:
        for var in (self.hist_search, self.hist_from, self.hist_to):
            var.set("")
        for var in (self.hist_kind, self.hist_status, self.hist_worker):
            var.set("Vše")

    def _selected_history_path(self) -> Path | None:
        sel = self.history_table.tree.selection()
        if not sel or self.cfg is None:
            return None
        return self.cfg.logs / f"{sel[0]}.json"

    def _show_history_log(self) -> None:
        path = self._selected_history_path()
        log_path = path.with_suffix(".log") if path else None
        text = log_path.read_text(encoding="utf-8") if log_path and log_path.exists() else ""
        state = "normal" if path else "disabled"
        self.btn_hist_log.configure(state=state)
        self.btn_hist_json.configure(state=state)
        self.history_detail.configure(text=str(log_path) if log_path else "Vyberte log v tabulce.")
        widget = self.history_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        # zvýraznění chyb a hledaných výrazů
        for tag, words in (("error", ["ERROR", "CHYBA"]), ("hit", self.hist_search.get().split())):
            for word in words:
                start = "1.0"
                while True:
                    pos = widget.search(word, start, "end", nocase=tag == "hit")
                    if not pos:
                        break
                    end = f"{pos}+{len(word)}c"
                    widget.tag_add(tag, pos, end if tag == "hit" else f"{pos} lineend")
                    start = end
        first_hit = widget.tag_nextrange("hit", "1.0")
        if first_hit:
            widget.see(first_hit[0])
        widget.configure(state="disabled")

    def _open_history_file(self, suffix: str) -> None:
        path = self._selected_history_path()
        if path:
            self._open(path.with_suffix(suffix))

    def _on_tab_changed(self) -> None:
        if self.busy or self.cfg is None:
            return
        tab = self.notebook.index("current")
        if tab == 1:
            self._refresh_transfer()
        elif tab == 2:
            self._refresh_history()

    def _update_buttons(self) -> None:
        ready = self.cfg is not None and not self.busy
        has_batch = ready and bool(self.cb_worker.get() and self.cb_batch.get())
        archive_ok = ready and is_dir(self.cfg.archive_root)
        self.btn_check.configure(state="normal" if has_batch else "disabled")
        self.btn_process.configure(state="normal" if has_batch and self.exiftool_ver else "disabled")
        self.btn_prepare.configure(state="normal" if archive_ok and self.transfer_groups else "disabled")
        self.btn_confirm.configure(state="normal" if archive_ok and self.pending_transfer else "disabled")

    # ------------------------------------------------------------------ běh na pozadí

    def _run_background(self, job, on_done, run_box: RunBox) -> None:
        self.busy = True
        self._update_buttons()
        self._current_run = run_box
        run_box.start()
        self.configure(cursor="watch")

        def progress(fraction: float, text: str) -> None:
            self.events.put(("progress", fraction, text))  # z vlákna nikdy nesahat přímo na Tk

        def runner():
            try:
                result = job(progress)
            except Exception as exc:  # noqa: BLE001
                if self.syslog:
                    self.syslog.exception("Neočekávaná chyba v operaci na pozadí")
                self.events.put(("crash", exc, on_done))
            else:
                self.events.put(("done", result, on_done))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, a, b = self.events.get_nowait()
                if kind == "progress" and self._current_run:
                    self._current_run.set_progress(a, b)
                elif kind == "exiftool":
                    self.exiftool_ver, self.exiftool_checked = a, True
                    self._render_status()
                    self._update_buttons()
                elif kind in ("done", "crash"):
                    if self._current_run:
                        self._current_run.stop()
                    self.busy = False
                    self.configure(cursor="")
                    if kind == "crash":
                        messagebox.showerror("Neočekávaná chyba",
                                             f"{a}\n\nPodrobnosti jsou v app_logs/system_runner.log.")
                    else:
                        b(a)
                    self._update_buttons()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _show_result(self, banner: Banner, result: workflow.BatchResult, stopped: str) -> None:
        """Výsledek zpracování/přenosu do banneru; při chybě navíc dialog."""
        if result.ok:
            banner.show("ok", f"{result.message}\nAudit: {result.json_log}")
        elif result.status == "BUSY":
            banner.show("warn", result.message)
        else:
            banner.show("error", f"{stopped}: {result.message}")
            messagebox.showerror(stopped, result.message)

    # ------------------------------------------------------------------ akce: zpracování

    def on_check(self) -> None:
        cfg, worker, batch = self.cfg, self.cb_worker.get(), self.cb_batch.get()
        preset = cfg.presets[self.cb_preset.get()]

        def job(progress):
            progress(0.2, "Kontroluji soubory…")
            return workflow.check_batch(cfg, worker, batch, preset)

        def done(result):
            checks, problems = result
            rows = [c.to_dict() for c in checks]
            bad = [r for r in rows if not r["ok"]]
            self.process_table.set_rows(file_rows(rows))
            if bad or problems:
                text = f"Kontrola nalezla problémy: {len(bad)} z {len(rows)} souborů nevyhovuje."
                if problems:
                    text += "\n" + "\n".join(problems)
                self.process_banner.show("error", text)
            else:
                self.process_banner.show("ok", f"Všech {len(rows)} souborů vyhovuje presetu. Dávku lze zpracovat.")

        self._run_background(job, done, self.process_run)

    def on_process(self) -> None:
        cfg, worker, batch = self.cfg, self.cb_worker.get(), self.cb_batch.get()
        preset = cfg.presets[self.cb_preset.get()]
        self.syslog.info("Spuštěno zpracování %s / %s (%s)", worker, batch, preset.name)

        def done(result: workflow.BatchResult):
            self.process_table.set_rows(file_rows(result.files))
            self.last_logs["process"] = result.text_log
            self.btn_process_log.configure(state="normal" if result.text_log else "disabled")
            self._refresh_workers()
            self._show_result(self.process_banner, result, "Dávka zastavena")

        self._run_background(lambda p: workflow.process_batch(cfg, worker, batch, preset, p), done,
                             self.process_run)

    # ------------------------------------------------------------------ akce: přenos (2 kroky)

    def _selected_transfer_files(self) -> list[workflow.PreparedFile]:
        return [f for label in self.transfer_table.tree.selection() for f in self.transfer_groups.get(label, [])]

    def on_prepare_transfer(self) -> None:
        """KROK 1 – jen připraví souhrn, nic se nekopíruje."""
        files = self._selected_transfer_files()
        if not files:
            messagebox.showwarning("Odeslání", "Vyberte alespoň jednu dávku.")
            return
        size = sum(f.size for f in files) / 1e6
        self.pending_transfer = [f.name for f in files]
        self.confirm_text.configure(text=(
            f"Chystáte se odeslat {len(files)} souborů ({size:.1f} MB) "
            f"z {len(self.transfer_table.tree.selection())} dávek "
            f"do {self.cfg.archive_root}.\n"
            "Po ověření kontrolních součtů v archivu budou soubory odstraněny z PREPARED."
        ))
        self.confirm_frame.pack(fill="x", pady=(10, 0))
        self._update_buttons()

    def _cancel_pending_transfer(self) -> None:
        self.pending_transfer = None
        self.confirm_frame.pack_forget()
        self._update_buttons()

    def on_confirm_transfer(self) -> None:
        """KROK 2 – výslovné potvrzení obsluhou spustí přenos."""
        names = self.pending_transfer
        if not names or names != [f.name for f in self._selected_transfer_files()]:
            self._cancel_pending_transfer()
            return
        cfg = self.cfg
        self._cancel_pending_transfer()
        self.syslog.info("Obsluha potvrdila přenos %d souborů do archivu", len(names))

        def done(result: workflow.BatchResult):
            self.last_logs["transfer"] = result.text_log
            self.btn_transfer_log.configure(state="normal" if result.text_log else "disabled")
            self._refresh_transfer()
            self._show_result(self.transfer_banner, result, "Přenos zastaven")

        self._run_background(lambda p: workflow.transfer_files(cfg, names, p), done, self.transfer_run)

    # ------------------------------------------------------------------ ostatní

    def open_paths_dialog(self) -> None:
        if self.busy:
            return
        if self.cfg is None:
            messagebox.showerror("Nastavení cest", "Nejdřív opravte chybu v konfiguraci (presets.json).")
            return
        PathsDialog(self, self.cfg, on_save=self._save_paths)

    def _save_paths(self, paths: dict[str, str]) -> None:
        try:
            target = save_user_paths(self.cfg.source_path, paths)
        except OSError as exc:
            messagebox.showerror("Nastavení cest", f"Nastavení nelze uložit: {exc}")
            return
        if self.syslog:
            self.syslog.info("Cesty uloženy do %s", target)
        self.reload()

    def _open(self, path: Path | None) -> None:
        if path and path.exists():
            try:
                open_in_system(path)
            except OSError as exc:
                messagebox.showerror("Nelze otevřít", str(exc))

    def _on_tk_exception(self, exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        if self.syslog:
            self.syslog.error("Neošetřená chyba GUI:\n%s", text)
        messagebox.showerror("Neočekávaná chyba", f"{exc}\n\nPodrobnosti jsou v app_logs/system_runner.log.")

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "Probíhá operace",
            "Právě probíhá zpracování nebo přenos. Ukončení teď může nechat soubory v nedokončeném stavu.\n\n"
            "Opravdu ukončit?",
            icon="warning",
        ):
            return
        if self.syslog:
            self.syslog.info("Aplikace ukončena")
        self.destroy()


def main() -> None:
    enable_high_dpi()
    app = App()
    if app.syslog:
        app.syslog.info("Aplikace spuštěna (v%s)", APP_VERSION)
    app.mainloop()


if __name__ == "__main__":
    main()
