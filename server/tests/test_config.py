"""Finding the project's configuration.

Every config-reading endpoint answers 500 when this goes wrong, including the
ones that never touch the database -- which makes a setup problem look like a
database problem. These tests pin the search order.
"""
import sys
from pathlib import Path

import pytest

from kaigyou_core import config as cfg


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("KAIGYOU_ROOT", raising=False)
    monkeypatch.delenv("KAIGYOU_CONFIG_DIR", raising=False)


def make_project(root: Path) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "sources.yaml").write_text("sources: {}\n", encoding="utf-8")
    (root / "config" / "scoring.yaml").write_text(
        "active_profile: default\nprofiles:\n  default:\n    label: t\n", encoding="utf-8")
    return root


# ------------------------------------------------------------- search order
def test_the_checkout_is_found_from_the_package_location():
    """The normal case: installed from a checkout, config sits beside it."""
    assert (cfg.repo_root() / "config" / "sources.yaml").is_file()


def test_an_explicit_root_wins(tmp_path, monkeypatch):
    project = make_project(tmp_path / "elsewhere")
    monkeypatch.setenv("KAIGYOU_ROOT", str(project))
    assert cfg.repo_root() == project
    assert cfg.sources_config() == {"sources": {}}


def test_the_root_is_found_by_walking_up_from_a_subdirectory(tmp_path, monkeypatch):
    """Running from server/ or web/ must still find config/."""
    project = make_project(tmp_path / "proj")
    deep = project / "server" / "kaigyou_etl" / "adapters"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    monkeypatch.setattr(cfg, "__file__", str(deep / "config.py"))
    assert cfg._search_upwards(deep) == project


def test_a_site_packages_install_falls_back_to_the_working_directory(tmp_path, monkeypatch):
    """A non-editable install puts the package inside the virtualenv.

    Three levels up from there is somewhere in Lib/, which has no config/. The
    checkout the operator is standing in should be used instead.
    """
    project = make_project(tmp_path / "checkout")
    site_packages = tmp_path / "venv" / "Lib" / "site-packages" / "kaigyou_core"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(cfg, "__file__", str(site_packages / "config.py"))
    monkeypatch.chdir(project)
    assert cfg.repo_root() == project


# ------------------------------------------------------------------- errors
def test_a_missing_config_names_the_file_it_looked_for(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIGYOU_ROOT", str(tmp_path / "nowhere"))
    with pytest.raises(cfg.ConfigNotFound) as exc:
        cfg.sources_config()
    assert "sources.yaml" in str(exc.value)


def test_config_not_found_is_a_file_not_found_error():
    """Callers that only catch OSError still behave sensibly."""
    assert issubclass(cfg.ConfigNotFound, FileNotFoundError)


# ------------------------------------------------------------------ reloading
def test_an_edited_file_is_re_read_without_a_restart(tmp_path, monkeypatch):
    """The API reloads scoring weights from disk; that is the mechanism."""
    import os
    import time

    project = make_project(tmp_path / "proj")
    monkeypatch.setenv("KAIGYOU_ROOT", str(project))
    assert cfg.scoring_config()["active_profile"] == "default"

    path = project / "config" / "scoring.yaml"
    path.write_text("active_profile: other\nprofiles:\n  other:\n    label: t\n",
                    encoding="utf-8")
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert cfg.scoring_config()["active_profile"] == "other"
