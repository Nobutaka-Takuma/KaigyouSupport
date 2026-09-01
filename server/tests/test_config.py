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


# --------------------------------------------- 設定を業態ごとに読み分けること
#
# 医科への拡張の 4 段目。業態の知識は共通化できないので、置き場所を分けて
# 読み分けます。詳細は docs/refactoring-multi-specialty.md。

def test_the_business_knowledge_lives_under_its_own_folder():
    """重み・複合指標・KSF・プロンプトは業態ごと。**フォルダ名は業態そのもの。**

    対応表（dental_clinic -> dental のような）を持つと、業態を足すたびに
    そこを直す必要が出て、忘れると設定が黙って読まれません。
    """
    from kaigyou_core import config as cfg
    from kaigyou_core.analysis import DEFAULT_CATEGORY

    assert cfg.business_dir().name == DEFAULT_CATEGORY
    for name in ("scoring.yaml", "insights.yaml", "hypotheses.yaml"):
        assert (cfg.business_dir() / name).is_file(), f"{name} が業態フォルダにない"
    assert (cfg.business_dir() / "prompts" / "step1_features.md").is_file()


def test_the_shared_files_are_not_split_by_business_type():
    """**データの出どころと段の構成は業態で変わりません。**

    分けると同じ国勢調査の定義を業態の数だけ複製することになり、片方だけ
    直したときに「同じ商圏なのに業態で人口が違う」が起きます。
    """
    from kaigyou_core import config as cfg

    assert (cfg.config_dir() / "sources.yaml").is_file()
    assert (cfg.config_dir() / "analysis.yaml").is_file()
    assert not (cfg.business_dir() / "sources.yaml").exists()
    assert not (cfg.business_dir() / "analysis.yaml").exists()


def test_a_business_type_without_a_folder_falls_back(tmp_path, monkeypatch):
    """設定を移していない環境を、この変更でその場で壊さないこと。

    **移行のための落とし先です。** 業態のフォルダがあればそちらが勝ちます。
    """
    from kaigyou_core import config as cfg

    (tmp_path / "scoring.yaml").write_text("active_profile: shared\n", encoding="utf-8")
    monkeypatch.setenv("KAIGYOU_CONFIG_DIR", str(tmp_path))
    assert cfg.scoring_config()["active_profile"] == "shared", \
        "業態フォルダが無ければ config/ 直下を読むこと"

    business = tmp_path / "dental_clinic"
    business.mkdir()
    (business / "scoring.yaml").write_text("active_profile: dental\n", encoding="utf-8")
    assert cfg.scoring_config()["active_profile"] == "dental", \
        "業態フォルダがあればそちらが勝つこと"
    assert cfg.scoring_config("clinic")["active_profile"] == "shared", \
        "別業態は、その業態のフォルダが無ければ共通に落ちる"


def test_every_step_reads_the_prompts_of_the_job_business_type():
    """医科のジョブが歯科のプロンプトで書かれないこと。

    **しかも成功と表示されます。** レポートを読むまで気づけません。
    渡し忘れを型で捕まえられないので、引数の名前で見張ります。
    """
    import inspect

    from kaigyou_intel.worker import RUNNERS

    for number, runner in RUNNERS.items():
        params = list(inspect.signature(runner).parameters)
        assert len(params) >= 2, f"STEP{number} が業態を受け取っていません"
        assert params[1] == "category", f"STEP{number}: {params}"


def test_a_connection_storm_is_retried_but_a_wrong_password_is_not():
    """**待てば取れるものを、待たずに失敗させない。**

    地図は 1 回動かすたびに層の数だけ並行に接続を取りにいき、その裏で cron が
    毎分 worker を叩きます（1 回は最大 13 分走れるので重なります）。山の
    てっぺんで接続が取れないと、画面には「レイヤーが出ない」「分析が進まない」
    として出ます。

    ただし認証の誤りは待っても直りません。間違ったパスワードで 3 回試すのは
    遅くなるだけなので、そちらは 1 回で諦めます。
    """
    from kaigyou_core.db import _is_transient

    for message in ("FATAL: sorry, too many clients already",
                    "connection reset by peer",
                    "server closed the connection unexpectedly",
                    "remaining connection slots are reserved"):
        assert _is_transient(message), message

    for message in ("password authentication failed for user \"postgres\"",
                    "permission denied for table facilities",
                    "database \"kaigyou\" does not exist"):
        assert not _is_transient(message), message
