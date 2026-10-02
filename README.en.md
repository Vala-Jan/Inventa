# Inventa

**Inventa – validation, metadata & ingest pipeline for museum digitization**

*This is an English translation. The primary, authoritative version is the [Czech README](README.md).*

A desktop application for Windows (a regular window – no server, localhost or browser) for digitizing
museum collections. It checks TIFF scans, writes Dublin Core metadata via ExifTool, keeps audit
logs and, when the operator says so, transfers finished files to a network archive.

![Batch processing](docs/zpracovani.png)

![Batch check with errors](docs/kontrola.png)

![Two-step transfer to the archive](docs/prenos.png)

## Location and structure

The recommended layout is shown below, but it is not mandatory. You set where the scans, the network archive
and other folders are directly in the application (the **Nastavit cesty…** / *Set paths…* button).

> The application UI is in Czech. Button and tab names are given in their original form, with an English
> translation in italics.

```text
D:\Digitalizační_pracoviště\
├── TOOLS\
│   └── Inventa\                <-- this repository
│       ├── config\presets.json  rules (presets, DPI, metadata, default paths)
│       ├── config\settings.json paths set in the application (created on first save)
│       ├── src\
│       │   ├── gui.py           desktop window (Tkinter – part of Python)
│       │   ├── validator.py     TIFF / DPI / color mode / filename checks (Pillow)
│       │   ├── metadata.py      metadata writing + read-back verification (ExifTool)
│       │   ├── logger.py        audit JSON + .log, system_runner.log
│       │   ├── workflow.py      controls the whole batch (check -> metadata -> PREPARED -> archive)
│       │   └── config.py        loads and validates presets.json
│       ├── tools\exiftool.exe   portable ExifTool (download separately, see tools\README.md)
│       ├── app_logs\            permanent audit (never overwritten)
│       ├── PREPARED\            only clean, processed TIFFs (flat)
│       └── Inventa.bat          launches the application (double-click)
└── SCANS\<INSTITUTION>\Pracovníci\<Worker>\<YYYY-MM-DD>\   input from the operator
```

## Requirements

* Windows 10 or 11
* Python 3.10 or newer (with Tkinter – included by default in the python.org installer)
* [ExifTool](https://exiftool.org/) (portable Windows version, downloaded separately)
* for transfers to the archive, a network drive mapped to a drive letter (default `Z:`)

## Installation (one-time)

1. Install **Python 3.10+** from [python.org](https://www.python.org/downloads/windows/)
   (keep *tcl/tk and IDLE* and *py launcher* checked – both are on by default).
2. Download the latest version from [Releases](../../releases/latest) – the **Source code (zip)** file.
3. Before extracting the ZIP: right-click → **Properties** → check **Unblock** → OK.
   (Otherwise Windows shows a SmartScreen warning for the downloaded `Inventa.bat`.)
4. Extract the ZIP, rename the resulting folder (e.g. `Inventa-0.2.0`) to `Inventa` and move it
   wherever you need – ideally to `D:\Digitalizační_pracoviště\TOOLS\`.
5. Put portable ExifTool into the `tools\` folder as described in [tools/README.md](tools/README.md).
6. Run `Inventa.bat` (double-click). On first run it creates `.venv` and installs the single
   dependency – Pillow (requires internet, takes about a minute). It then opens the application window; the black console closes by itself.
7. On first run, the **Nastavení cest** (*Path settings*) window opens. Use the **Procházet…** (*Browse…*) button to select:
   * **Vstupní složka se skeny** (*Input folder with scans*) – the folder where each worker has their own subfolder with batches,
     e.g. `D:\Digitalizační_pracoviště\SCANS\MUZEUM\Pracovníci`,
   * **Síťový archiv** (*Network archive*) – where finished files are sent, e.g. `Z:\Digitalizace`.

   PREPARED, logs and ExifTool have sensible defaults inside the application folder; there is no need to change them.
   Paths can be changed at any time with the **Nastavit cesty…** (*Set paths…*) button in the left panel; they are saved to `config\settings.json`.
8. In the left panel **Stav systému** (*System status*), all items should be green. A red item shows
   which path or tool needs fixing.

Tip: create a desktop shortcut to `Inventa.bat`.

**Updating to a new version:** download the new ZIP and overwrite the `Inventa` folder with it. Your paths
(`config\settings.json`), ExifTool (`tools\`) and audit (`app_logs\`) will not be overwritten by the new ZIP, because they are not in it.
If you have edited the presets, back up `config\presets.json` first.

## Batch lifecycle

| Step | What happens |
|---|---|
| 1. Input | The operator uploads TIFFs to `SCANS\<INSTITUTION>\Pracovníci\<Name>\<YYYY-MM-DD>\`. |
| 2. Selection | In the application window, you choose the worker, batch and preset. |
| 2b. Dry run | The **Zkontrolovat (bez zápisu)** (*Check (no write)*) button goes through *all* files and shows all errors at once. It changes nothing and does not write to the audit. |
| 3. Validation | **Zpracovat dávku** (*Process batch*): `.tif/.tiff` extension, actual TIFF format, DPI ≥ minimum, color mode, (optionally) bit depth and compression, filename format and inventory number extraction (see below). The first error stops the batch immediately (red message + error dialog + details in the log). |
| 4. Metadata | Only once *all* files pass does ExifTool write `XMP-dc:Identifier` and `XMP-dc:Description`. The write is immediately read back and verified. SHA-256 is computed both before and after the write. |
| 5. PREPARED | TIFFs are moved flat into `PREPARED\` (`Thumbs.db` and similar are discarded, the empty batch folder is removed). If a file with the same name already exists in PREPARED, the batch stops before anything is changed. |
| 6. Audit | `app_logs\runner_YYYY-MM-DD_Firstname-Lastname.json` (machine-readable, for SQL import) + `.log` (timeline). Repeated runs create `_run2`, `_run3`, … Nothing is overwritten. |
| 7. Archive | Never happens automatically. In the **Odeslání do archivu** (*Send to archive*) tab, the operator selects batches, clicks **Připravit odeslání** (*Prepare transfer*), reviews the summary, and only the second step **Potvrdit a odeslat** (*Confirm and send*) starts the transfer: robocopy to `Z:\…` (flat), SHA-256 verification at the destination against the audit, then deletion from PREPARED. Log `upload_YYYY-MM-DD.json/.log`. |

## Presets (`config/presets.json`)

```jsonc
"Diapozitivy 600 DPI": {
  "min_dpi": 600,
  "allowed_color_modes": ["RGB", "Grayscale"],   // RGB, Grayscale, Bitonal, CMYK, RGBA
  "allowed_bit_depths": null,                     // e.g. [8, 16]; null = not checked
  "allowed_compressions": null,                   // e.g. ["raw", "tiff_lzw"]
  "deep_check": false,                            // true = full decode (detects corrupted files, slower)
  "metadata": {
    "XMP-dc:Identifier": "{inventory}",
    "XMP-dc:Description": "{inventory}"          // ABC-01X-DIA001--001.tif -> "DIA001"
  }
}
```

* The inventory number is always taken from the filename using the same rule for all presets:
  the segment between the last `-` and `--`. Filename format: `<anything>-<inventory number>--<sequence>.tif`.

  | Filename | Inventory number |
  |---|---|
  | `ABC-01X-DIA001--001.tif` | `DIA001` |
  | `ABC-01X-FOT123--002.tif` | `FOT123` |
  | `DIA001--001.tif`, `ABC-01X-DIA001-001.tif` | error – cannot be determined |

* Metadata templates can use `{inventory}`, `{prefix}` (the part before the inventory number), `{sequence}` (sequence number),
  `{worker}`, `{batch}`, `{preset}` and `{filename}`.
* The application reports errors in `presets.json` in red, including a description (line, unknown mode, …).
* A full copy of the preset is saved into every JSON log, so the audit stays understandable even after the rules change.

## History and logs

The **Historie a logy** (*History and logs*) tab shows all audit records from `app_logs`:

* **Type** `runner` = batch processing, `upload` = transfer to the archive.
* **Hledat** (*Search*) – searches log names, workers, batches, presets, filenames, inventory numbers
  and error messages (multiple words = all must match). `Esc` clears the search.
* **Filters** by type, status, worker and date (from–to), plus a **Zrušit filtry** (*Clear filters*) button.
* Clicking a column header sorts (▲/▼); the default is newest first.

![History and logs](docs/historie.png)

* The selected log is shown at the bottom – error lines in red, the search term highlighted in yellow. Double-click or
  **Otevřít .log / .json** (*Open .log / .json*) opens the file in the system.

## Security and operational notes

* Diacritics in paths (`Digitalizační_pracoviště`): ExifTool on Windows cannot handle Unicode
  command-line arguments, so parameters are passed via a UTF-8 argfile (`-@`) and `-charset filename=utf8`.
* Transfers to the archive run in the default `verify_then_delete` mode: robocopy copies the files,
  the application verifies SHA-256 directly on `Z:` and only then deletes the source in PREPARED. The `robocopy_move` mode
  (plain `/MOV`) is available, but if verification failed the source would no longer exist.
* A transfer rejects any file that has no successful audit record, or that has changed since processing.
  It never overwrites a different file with the same name in the archive; an identical copy (e.g. after an interrupted transfer) is only verified.
* The application is purely desktop: it opens no network port and sends nothing anywhere except the network archive.
* The window stays responsive during processing/transfer (work runs in the background). Closing the window mid-operation
  requires confirmation.
* Writing metadata modifies TIFFs in place (`-overwrite_original`). If a batch fails during the metadata
  write, some files may already have metadata. Re-processing is safe (the same values are written).

## Sample data

The screenshots in `docs/` were taken with fictitious data (made-up workers, institutions and inventory numbers).

## Development

```bash
python -m pip install -r requirements.txt   # on Windows, plain "pip" is often not on PATH
python src/gui.py                           # runs the window with a console (so you can see any errors)
```

## License

[MIT](LICENSE) – you may freely use, modify and distribute the application; just keep the license text.
No warranty: before using it on production data, make sure the presets match your collection's rules.
