"""Kouřový test desktopového okna (přeskočí se, pokud není k dispozici Tkinter nebo obrazovka)."""

import time

import pytest
from conftest import PRESET, batch_dir, make_tiff, requires_exiftool

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(workspace, monkeypatch):
    try:
        tk.Tk().destroy()
    except tk.TclError:
        pytest.skip("Není dostupná grafická obrazovka")
    import gui

    monkeypatch.setattr(gui, "DEFAULT_CONFIG_PATH", workspace.source_path)
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg, **kw: errors.append(msg))
    window = gui.App()
    window.errors = errors
    yield window
    window.destroy()


def wait(app, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if not app.busy and app.exiftool_checked:
            break
        time.sleep(0.05)
    app.update()


@requires_exiftool
def test_process_and_two_step_transfer(app, workspace):
    for i in (1, 2):
        make_tiff(batch_dir(workspace) / f"ABC-01X-DIA001--{i:03d}.tif")
    app.reload()
    wait(app)
    assert app.cb_worker.get() == "Jana Nováková" and app.cb_batch.get() == "2026-09-29"

    app.btn_process.invoke()
    wait(app)
    assert not app.errors
    assert len(list(workspace.prepared.glob("*.tif"))) == 2

    app.notebook.select(1)
    wait(app)
    assert str(app.btn_confirm["state"]) == "disabled"  # bez kroku 1 nelze potvrdit
    app.btn_prepare.invoke()
    app.update()
    assert list(workspace.archive_root.iterdir()) == []  # krok 1 nic nekopíruje
    app.btn_confirm.invoke()
    wait(app)
    assert len(list(workspace.archive_root.iterdir())) == 2
    assert not app.errors


def test_failed_batch_shows_error(app, workspace):
    make_tiff(batch_dir(workspace) / "spatny_nazev.tif")
    app.reload()
    wait(app)
    app.btn_check.invoke()
    wait(app)
    assert "problémy" in app.process_banner.label.cget("text")


def history_rows(app):
    tree = app.history_table.tree
    return [dict(zip(app.history_table.keys, tree.item(i, "values"))) for i in tree.get_children()]


def test_history_types_filters_and_search(app, workspace):
    import workflow

    make_tiff(batch_dir(workspace) / "ABC-01X-DIA001--001.tif", dpi=(100, 100))  # -> FAILED runner
    workflow.process_batch(workspace, "Jana Nováková", "2026-09-29", workspace.presets[PRESET])
    make_tiff(workspace.prepared / "ABC-01X-XYZ999--001.tif")
    workflow.transfer_files(workspace, ["ABC-01X-XYZ999--001.tif"])  # -> FAILED upload (bez auditu)
    app.reload()
    app._refresh_history()
    app.update()

    rows = history_rows(app)
    assert sorted(r["typ"] for r in rows) == ["runner", "upload"]
    assert "Zobrazeno 2 z 2" in app.history_count.cget("text")

    app.hist_kind.set("upload")
    app._apply_history_filter()
    assert [r["typ"] for r in history_rows(app)] == ["upload"]

    app._reset_history_filters()
    app.hist_search.set("dia001")  # inventární číslo z obsahu logu, bez ohledu na velikost písmen
    app._apply_history_filter()
    assert [r["typ"] for r in history_rows(app)] == ["runner"]

    app.hist_search.set("nesmysl")
    app._apply_history_filter()
    assert history_rows(app) == []

    app._reset_history_filters()
    app.hist_worker.set("Jana Nováková")
    app._apply_history_filter()
    assert [r["pracovnik"] for r in history_rows(app)] == ["Jana Nováková"]
    assert app.title() == "Inventa"
