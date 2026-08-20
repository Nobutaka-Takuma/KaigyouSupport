"""Behaviour that only differs once the app is not on the operator's laptop.

Two settings change between a local run and a hosted one, and both fail
quietly if they are wrong: a pooled connection that still prepares statements
answers correctly until PgBouncer happens to reuse a backend, and a 500 that
names its exception is a debugging aid locally and a disclosure publicly.
"""
from __future__ import annotations

import pytest

from kaigyou_api import main as api_main
from kaigyou_core import db


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for name in ("KAIGYOU_DB_PREPARE", "KAIGYOU_ERROR_DETAIL",
                 "VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------- pooled connections
@pytest.mark.parametrize("url, pooled", [
    # Supabase's transaction pooler: statements must not be prepared.
    ("postgresql://u:p@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres", True),
    # Its session pooler holds one backend per client, but shares the hostname;
    # treating it as pooled only costs a little planning time.
    ("postgresql://u:p@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres", True),
    ("postgresql://u:p@host/db?pgbouncer=true", True),
    # A direct connection, hosted or local, prepares as usual.
    ("postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres", False),
    ("postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou", False),
])
def test_a_pooler_is_recognised_from_its_dsn(url, pooled):
    assert db.is_pooled(url) is pooled


def test_the_operator_can_override_the_detection(monkeypatch):
    monkeypatch.setenv("KAIGYOU_DB_PREPARE", "off")
    assert db.is_pooled("postgresql://u:p@127.0.0.1:5432/db") is True
    monkeypatch.setenv("KAIGYOU_DB_PREPARE", "on")
    assert db.is_pooled("postgresql://u:p@host:6543/db") is False


def test_a_malformed_dsn_does_not_raise():
    """A bad DATABASE_URL should fail at connect, with psycopg's message."""
    assert db.is_pooled("postgresql://u:p@host:not-a-port/db") is False


# --------------------------------------------------------- error disclosure
def test_exceptions_are_named_when_running_locally():
    assert api_main.expose_error_detail() is True


@pytest.mark.parametrize("marker", ["VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE"])
def test_exceptions_are_withheld_on_a_hosting_platform(monkeypatch, marker):
    monkeypatch.setenv(marker, "1")
    assert api_main.expose_error_detail() is False


def test_the_operator_can_ask_for_detail_back(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("KAIGYOU_ERROR_DETAIL", "1")
    assert api_main.expose_error_detail() is True


def test_and_can_turn_it_off_locally(monkeypatch):
    monkeypatch.setenv("KAIGYOU_ERROR_DETAIL", "off")
    assert api_main.expose_error_detail() is False


# ------------------------------------------------------------ batched loads
class RecordingCursor:
    """Counts round trips the way the network sees them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def executemany(self, sql, rows):
        self.calls.append(("executemany", len(list(rows))))

    def execute(self, sql, params=None):
        self.calls.append(("execute", 1))


def test_rows_go_out_in_batches_not_one_at_a_time():
    """51,384 clinics at one round trip each is thirteen minutes of latency."""
    from kaigyou_etl.adapters.base import BATCH_ROWS, SourceAdapter

    cur = RecordingCursor()
    rows = [{"n": i} for i in range(BATCH_ROWS * 2 + 7)]
    assert SourceAdapter.insert_many(cur, "INSERT ...", rows) == len(rows)

    assert [c[0] for c in cur.calls] == ["executemany"] * 3
    assert [c[1] for c in cur.calls] == [BATCH_ROWS, BATCH_ROWS, 7]


def test_no_rows_means_no_round_trips():
    from kaigyou_etl.adapters.base import SourceAdapter

    cur = RecordingCursor()
    assert SourceAdapter.insert_many(cur, "INSERT ...", []) == 0
    assert cur.calls == []


# ------------------------------------------------------- the entry point
def test_the_entry_point_exposes_app_at_module_level():
    """Vercel reads api/index.py; it does not run it.

    Detection is static: the platform parses the file looking for a
    module-level name ``app``. Wrapping the import in try/except -- which is
    tempting, because an import failure otherwise reaches the browser as an
    unexplained FUNCTION_INVOCATION_FAILED -- moves the binding inside a Try
    node, where the detector does not look. The build then fails with "does
    not define a top-level 'app' FastAPI instance", which sounds like a
    problem with the application and is not one.
    """
    import ast
    from pathlib import Path

    entry = Path(__file__).resolve().parents[2] / "api" / "index.py"
    tree = ast.parse(entry.read_text(encoding="utf-8"))

    bound = []
    for node in tree.body:  # module level only, as the detector does
        if isinstance(node, ast.ImportFrom):
            bound += [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, ast.Assign):
            bound += [t.id for t in node.targets if isinstance(t, ast.Name)]

    assert "app" in bound, (
        "api/index.py must bind 'app' at module level or Vercel's build fails; "
        f"top-level names are {bound}"
    )


def test_the_entry_point_actually_serves_the_application():
    """Statically visible is necessary but not sufficient -- it must import."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from api.index import app

    from kaigyou_api.main import app as real_app

    assert app is real_app


# ------------------------------------------------------------------- health
def test_health_reports_the_settings_without_printing_them(monkeypatch):
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    secret = "postgresql://postgres:hunter2@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"
    monkeypatch.setenv("DATABASE_URL", secret)
    body = TestClient(app).get("/api/health").json()

    assert body["status"] == "ok"
    assert body["database_url_set"] is True
    assert body["database_pooled"] is True
    assert body["config_found"] is True
    # The whole point is that this is safe to paste into a chat window.
    assert "hunter2" not in str(body)


def test_health_says_so_when_the_database_url_is_missing(monkeypatch):
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    body = TestClient(app).get("/api/health").json()
    assert body["database_url_set"] is False
    assert body["database_pooled"] is None


# ------------------------------------------------- unreachable direct host
@pytest.mark.parametrize("message", [
    # Windows
    "failed to resolve host 'db.abc.supabase.co': [Errno 11001] getaddrinfo failed",
    # Linux
    "failed to resolve host 'db.abc.supabase.co': [Errno -2] Name or service not known",
    # Half-configured IPv6
    "connection failed: Network is unreachable",
    # libpq with nothing to say for itself
    "connection is bad: no error details available",
])
def test_the_ipv6_only_direct_host_is_explained_however_it_fails(message):
    """The wording differs per platform, so the rule cannot depend on it."""
    hint = db.connection_hint(
        "postgresql://postgres:pw@db.abc.supabase.co:5432/postgres", message)
    assert hint is not None
    assert "Session pooler" in hint


def test_a_pooler_host_is_left_alone():
    """It reached the right host; whatever went wrong, this is not the advice."""
    assert db.connection_hint(
        "postgresql://postgres.abc:pw@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres",
        "connection failed: timeout expired") is None


def test_a_local_database_is_left_alone():
    assert db.connection_hint(
        "postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou",
        "connection refused") is None


@pytest.mark.parametrize("message", [
    'password authentication failed for user "postgres"',
    'database "postgres" does not exist',
    'permission denied for schema public',
])
def test_getting_far_enough_to_be_rejected_means_the_host_was_fine(message):
    """Being told no is proof the address resolved -- do not blame the address."""
    assert db.connection_hint(
        "postgresql://postgres:pw@db.abc.supabase.co:5432/postgres", message) is None


# ------------------------------------------- staying alive to be diagnosed
def test_health_answers_and_names_the_cause_when_routers_fail(monkeypatch):
    """A broken import must not take the whole application down.

    On a serverless platform an exception during module import happens before
    any handler exists, so the request dies as FUNCTION_INVOCATION_FAILED with
    nothing to go on. Keeping `app` alive costs one try/except and turns that
    into an answer.
    """
    import builtins
    import importlib
    import sys

    from fastapi.testclient import TestClient  # imports httpx before the guard

    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.startswith("kaigyou_api.routers"):
            raise ModuleNotFoundError("No module named 'psycopg'")
        return real_import(name, *args, **kwargs)

    for module in [m for m in sys.modules if m.startswith("kaigyou_api")]:
        del sys.modules[module]
    monkeypatch.setattr(builtins, "__import__", guard)
    broken = importlib.import_module("kaigyou_api.main")
    monkeypatch.setattr(builtins, "__import__", real_import)

    body = TestClient(broken.app, raise_server_exceptions=False).get("/api/health")
    assert body.status_code == 200
    assert body.json()["status"] == "degraded"
    assert body.json()["routers_loaded"] is False
    assert "psycopg" in body.json()["router_error"]

    # Leave the module registry as we found it, or later tests get the stub.
    for module in [m for m in sys.modules if m.startswith("kaigyou_api")]:
        del sys.modules[module]
    importlib.import_module("kaigyou_api.main")


# --------------------------------------------------- what the platform installs
def test_requirements_names_no_local_paths():
    """A local path requirement cannot be installed by Vercel's installer.

    uv derives the package name from the directory, so `./server` is looked for
    under the name `server` while the distribution calls itself
    `kaigyou-support`, and the build fails on the mismatch:

        x Failed to build `server @ file:///vercel/path0/server`
        `-> Package metadata name `kaigyou-support` does not match given name `server`

    Our packages reach the function as source instead -- see vercel.json's
    includeFiles and the sys.path line in api/index.py.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    lines = [line.strip() for line in
             (root / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    requirements = [line for line in lines if line and not line.startswith("#")]

    assert requirements, "requirements.txt lists nothing"
    for line in requirements:
        assert not line.startswith((".", "/", "-e")), (
            f"{line!r} is a local path; Vercel's uv-based install rejects these"
        )
        assert "file:" not in line, f"{line!r} points at the filesystem"


def test_the_bundle_carries_the_source_the_entry_point_expects():
    """includeFiles must cover both config/ and server/, or nothing imports."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
    included = config["functions"]["api/index.py"]["includeFiles"]

    assert "config" in included, "config/*.yaml would not reach the function"
    assert "server" in included, (
        "server/ would not reach the function, and it is not installed either"
    )


# --------------------------------------------------------- serving the SPA
@pytest.fixture
def client_with_web(tmp_path, monkeypatch):
    """An app whose bundle contains a built web client."""
    import importlib
    import sys

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "assets" / "main.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not yours", encoding="utf-8")

    monkeypatch.setenv("KAIGYOU_WEB_DIST", str(dist))
    for module in [m for m in sys.modules if m.startswith("kaigyou_api")]:
        del sys.modules[module]
    main = importlib.import_module("kaigyou_api.main")

    from fastapi.testclient import TestClient
    yield TestClient(main.app, raise_server_exceptions=False)

    for module in [m for m in sys.modules if m.startswith("kaigyou_api")]:
        del sys.modules[module]
    importlib.import_module("kaigyou_api.main")


def test_the_root_serves_the_web_client(client_with_web):
    """Vercel hands every request to the app, "/" included.

    Before this, the front page answered {"detail":"Not Found"} -- the API is
    correct to have nothing at the root, but that is not what the reader wants.
    """
    response = client_with_web.get("/")
    assert response.status_code == 200
    assert "<!doctype html>" in response.text


def test_client_side_routes_get_the_page_not_a_404(client_with_web):
    """/ranking is a route inside the SPA; the server has never heard of it."""
    assert "<!doctype html>" in client_with_web.get("/ranking").text


def test_real_assets_are_served_as_themselves(client_with_web):
    response = client_with_web.get("/assets/main.js")
    assert response.status_code == 200
    assert response.text == "console.log(1)"


def test_a_mistyped_api_path_gets_json_not_the_web_page(client_with_web):
    """Answering an API typo with HTML hides the mistake from whoever made it."""
    response = client_with_web.get("/api/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "/api/nope" in response.json()["detail"]


def test_the_catch_all_cannot_be_walked_out_of(client_with_web):
    response = client_with_web.get("/../../secret.txt")
    assert "not yours" not in response.text


def test_health_reports_whether_the_client_was_bundled(client_with_web):
    assert client_with_web.get("/api/health").json()["web_client_bundled"] is True


def test_no_rewrite_can_swallow_an_asset_request():
    """A catch-all rewrite to /index.html serves HTML where JS was expected.

    `/((?!api/).*)` -> `/index.html` looks like a reasonable SPA fallback, and
    is one when the platform checks the filesystem first. Vercel does not, for
    a project whose backend framework it has detected: it routes by the
    destination path, so a request for /assets/index-abc123.js is answered with
    the page. The browser reports

        Failed to load module script: Expected a JavaScript-or-Wasm module
        script but the server responded with a MIME type of "text/html"

    and renders nothing at all -- a blank page with a working API behind it.

    The SPA fallback belongs in the application (see main.py's catch-all),
    which serves a real file whenever one exists and index.html only when none
    does.
    """
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))

    for rewrite in config.get("rewrites", []):
        if not rewrite.get("destination", "").endswith(".html"):
            continue
        pattern = re.compile(rewrite["source"].rstrip("$") + "$")
        assert not pattern.match("/assets/index-abc123.js"), (
            f"rewrite {rewrite['source']!r} -> {rewrite['destination']!r} also "
            "matches asset requests, which serves them the page instead"
        )
