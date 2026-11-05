"""Phase 3: results viewer API, served from a real result written by the engine."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from pagereuse.experiment import ExperimentConfig, run
from pagereuse.export import write_result
from pagereuse.server import WEB_DIR, index_runs, make_server


@pytest.fixture(scope="module")
def results_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("results")
    write_result(run(ExperimentConfig(table="prl_test_srv", rows=500, seed=3, label="srv")), d)
    (d / "not-a-run").mkdir()
    (d / "not-a-run" / "result.json").write_text("{}")
    return d


@pytest.fixture(scope="module")
def base_url(results_dir):
    httpd = make_server(results_dir, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def test_index_skips_foreign_directories(results_dir):
    idx = index_runs(results_dir)
    assert len(idx) == 1
    assert idx[0]["label"] == "srv"
    assert idx[0]["steps"] == ["00_insert", "01_delete", "02_vacuum", "03_insert"]
    assert len(idx[0]["main_pages"]) == 4


def test_api_runs_and_run(base_url):
    status, ctype, body = get(f"{base_url}/api/runs")
    assert status == 200 and ctype.startswith("application/json")
    runs = json.loads(body)
    run_id = runs[0]["run_id"]
    status, _, body = get(f"{base_url}/api/runs/{run_id}")
    full = json.loads(body)
    assert full["run_id"] == run_id
    assert full["checkpoints"][0]["pages"][0]["blkno"] == 0


@pytest.mark.parametrize("bad", ["nope", "../etc/passwd", "20000101T000000Z-zzzzzz"])
def test_api_rejects_bad_run_ids(base_url, bad):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(f"{base_url}/api/runs/{bad}")
    assert e.value.code == 404


def test_static_ui_served(base_url):
    for name, needle in (("", b'<canvas id="heat"'), ("app.js", b"renderHeat"), ("style.css", b"--accent")):
        status, _, body = get(f"{base_url}/{name}")
        assert status == 200 and needle in body, name
    assert (WEB_DIR / "index.html").is_file()
