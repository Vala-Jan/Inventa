import json
import subprocess

from conftest import PRESET, batch_dir, make_tiff, requires_exiftool

import workflow

WORKER, BATCH = "Jana Nováková", "2026-09-29"


def read_tags(path):
    out = subprocess.run(["exiftool", "-j", "-G1", "-XMP-dc:all", str(path)], capture_output=True, check=True)
    return json.loads(out.stdout)[0]


def test_listing(workspace):
    make_tiff(batch_dir(workspace, "Petr Svoboda", "2026-09-28") / "ABC-01X-DIA002--001.tif")
    batch_dir(workspace, WORKER, BATCH)
    assert workflow.list_workers(workspace) == ["Jana Nováková", "Petr Svoboda"]
    assert workflow.list_batches(workspace, "Petr Svoboda") == ["2026-09-28"]


def test_check_batch_reports_all_problems(workspace):
    d = batch_dir(workspace)
    make_tiff(d / "ABC-01X-DIA001--001.tif", dpi=(300, 300))
    make_tiff(d / "spatny_nazev.tif")
    (d / "poznamka.txt").write_text("x")
    checks, problems = workflow.check_batch(workspace, WORKER, BATCH, workspace.presets[PRESET])
    assert [c.ok for c in checks] == [False, False]
    assert any("poznamka.txt" in p for p in problems)


def names(folder):
    return sorted(p.name for p in folder.iterdir())


@requires_exiftool
def test_full_lifecycle(workspace):
    d = batch_dir(workspace)
    for i in (1, 2, 10):
        make_tiff(d / f"ABC-01X-DIA001--{i:03d}.tif")
    (d / "Thumbs.db").write_bytes(b"junk")

    result = workflow.process_batch(workspace, WORKER, BATCH, workspace.presets[PRESET])
    assert result.ok, result.message
    assert not d.exists()

    # PREPARED je plochý – přímo TIFFy
    tiffs = ["ABC-01X-DIA001--001.tif", "ABC-01X-DIA001--002.tif", "ABC-01X-DIA001--010.tif"]
    assert names(workspace.prepared) == tiffs
    tags = read_tags(workspace.prepared / tiffs[0])
    assert tags["XMP-dc:Identifier"] == "DIA001"
    assert tags["XMP-dc:Description"] == "DIA001"

    log = json.loads(result.json_log.read_text(encoding="utf-8"))
    assert result.json_log.name == "runner_2026-09-29_Jana-Nováková.json"
    assert log["status"] == "SUCCESS" and log["preset"]["min_dpi"] == 600
    f0 = log["files"][0]
    assert f0["sha256_before"] != f0["sha256_after"]
    assert f0["exiftool"]["returncode"] == 0 and "-XMP-dc:Identifier=DIA001" in f0["exiftool"]["argfile_args"]
    assert "ČASOVÁ OSA" in result.text_log.read_text(encoding="utf-8")

    # zpracování samo nic do archivu neposílá
    assert names(workspace.archive_root) == []

    prepared = workflow.list_prepared(workspace)
    assert [(f.name, f.worker, f.batch, f.audited) for f in prepared] == [(n, WORKER, BATCH, True) for n in tiffs]

    # Explorer může do PREPARED přidat Thumbs.db – přenos to nesmí zablokovat
    (workspace.prepared / "Thumbs.db").write_bytes(b"junk")
    t = workflow.transfer_files(workspace, [f.name for f in prepared])
    assert t.ok, t.message
    assert names(workspace.archive_root) == tiffs  # archiv naplocho
    assert names(workspace.prepared) == []
    tl = json.loads(t.json_log.read_text(encoding="utf-8"))
    assert t.json_log.name.startswith("upload_") and t.json_log.suffix == ".json"
    assert all(f["ok"] and f["target_sha256"] == f["sha256"] for f in tl["files"])


@requires_exiftool
def test_prepared_name_clash_stops_batch(workspace):
    make_tiff(batch_dir(workspace) / "ABC-01X-DIA001--001.tif")
    assert workflow.process_batch(workspace, WORKER, BATCH, workspace.presets[PRESET]).ok
    d2 = batch_dir(workspace, "Petr Svoboda", "2026-09-30")
    src = make_tiff(d2 / "ABC-01X-DIA001--001.tif")
    before = src.read_bytes()
    r = workflow.process_batch(workspace, "Petr Svoboda", "2026-09-30", workspace.presets[PRESET])
    assert not r.ok and "stejným názvem" in r.message
    assert src.read_bytes() == before


@requires_exiftool
def test_first_error_stops_batch_without_changes(workspace):
    d = batch_dir(workspace)
    good = make_tiff(d / "ABC-01X-DIA001--001.tif")
    make_tiff(d / "ABC-01X-DIA001--002.tif", dpi=(150, 150))
    before = good.read_bytes()

    result = workflow.process_batch(workspace, WORKER, BATCH, workspace.presets[PRESET])
    assert not result.ok and "Nízké rozlišení" in result.message
    assert good.read_bytes() == before  # metadata se nezapisují, dokud neprojde celá dávka
    assert names(workspace.prepared) == []
    log = json.loads(result.json_log.read_text(encoding="utf-8"))
    assert log["status"] == "FAILED" and log["errors"]

    # opakovaný pokus po opravě -> nový log, starý se nepřepíše
    make_tiff(d / "ABC-01X-DIA001--002.tif")
    again = workflow.process_batch(workspace, WORKER, BATCH, workspace.presets[PRESET])
    assert again.ok, again.message
    assert again.json_log.name == "runner_2026-09-29_Jana-Nováková_run2.json"


def test_rejects_subfolders_and_other_files(workspace):
    d = batch_dir(workspace)
    make_tiff(d / "ABC-01X-DIA001--001.tif")
    (d / "podslozka").mkdir()
    result = workflow.process_batch(workspace, WORKER, BATCH, workspace.presets[PRESET])
    assert not result.ok and "podslozka" in result.message


def _processed(workspace):
    make_tiff(batch_dir(workspace) / "ABC-01X-DIA001--001.tif")
    assert workflow.process_batch(workspace, WORKER, BATCH, workspace.presets[PRESET]).ok
    return workspace.prepared / "ABC-01X-DIA001--001.tif"


@requires_exiftool
def test_transfer_detects_tampering(workspace):
    f = _processed(workspace)
    f.write_bytes(f.read_bytes() + b"x")
    t = workflow.transfer_files(workspace, [f.name])
    assert not t.ok and "SHA-256" in t.message
    assert f.exists() and names(workspace.archive_root) == []


@requires_exiftool
def test_transfer_refuses_different_file_in_archive(workspace):
    f = _processed(workspace)
    (workspace.archive_root / f.name).write_bytes(b"jiny obsah")
    t = workflow.transfer_files(workspace, [f.name])
    assert not t.ok and "JINÝ soubor" in t.message
    assert f.exists() and (workspace.archive_root / f.name).read_bytes() == b"jiny obsah"


@requires_exiftool
def test_transfer_skips_identical_copy_in_archive(workspace):
    f = _processed(workspace)
    (workspace.archive_root / f.name).write_bytes(f.read_bytes())  # např. po přerušeném přenosu
    t = workflow.transfer_files(workspace, [f.name])
    assert t.ok, t.message
    assert not f.exists()


def test_transfer_without_audit_refused(workspace):
    make_tiff(workspace.prepared / "ABC-01X-DIA001--001.tif")
    assert not workflow.list_prepared(workspace)[0].audited
    t = workflow.transfer_files(workspace, ["ABC-01X-DIA001--001.tif"])
    assert not t.ok and "auditní záznam" in t.message
