"""Configuration loading.

Both YAML files under ``config/`` are reloaded when their mtime changes, so
editing the scoring weights takes effect without restarting the API.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

_LOCK = threading.Lock()
_CACHE: dict[Path, tuple[float, dict[str, Any]]] = {}


#: The file whose presence identifies the project root.
_MARKER = Path("config") / "sources.yaml"


def _search_upwards(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / _MARKER).is_file():
            return candidate
    return None


def repo_root() -> Path:
    """Locate the project root, i.e. the directory holding ``config/``.

    Walking up beats a fixed number of ``parents`` hops: with a non-editable
    install the package sits in site-packages, three levels up from which is
    somewhere inside the virtualenv. Every config read then raises
    FileNotFoundError and the API answers 500 to requests that never touched
    the database -- a confusing failure for what is really a setup problem.
    """
    env = os.getenv("KAIGYOU_ROOT")
    if env:
        return Path(env).resolve()

    # Installed from a checkout: server/kaigyou_core/config.py -> repo root.
    from_package = _search_upwards(Path(__file__).resolve().parent)
    if from_package is not None:
        return from_package

    # Installed into site-packages but run from inside the checkout.
    from_cwd = _search_upwards(Path.cwd().resolve())
    if from_cwd is not None:
        return from_cwd

    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return Path(os.getenv("KAIGYOU_CONFIG_DIR") or (repo_root() / "config"))


def data_dir() -> Path:
    d = Path(os.getenv("KAIGYOU_DATA_DIR") or (repo_root() / "data"))
    return d


class ConfigNotFound(FileNotFoundError):
    """A configuration file could not be located, with somewhere to look."""


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, re-reading it only when it has changed on disk."""
    path = path.resolve()
    if not path.is_file():
        raise ConfigNotFound(f"設定ファイルが見つかりません: {path}")
    mtime = path.stat().st_mtime
    with _LOCK:
        cached = _CACHE.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    with _LOCK:
        _CACHE[path] = (mtime, data)
    return data


def scoring_config() -> dict[str, Any]:
    return load_yaml(config_dir() / "scoring.yaml")


def sources_config() -> dict[str, Any]:
    return load_yaml(config_dir() / "sources.yaml")


def analysis_config() -> dict[str, Any]:
    """商圏インテリジェンス・エンジンの設定。無い環境では空。"""
    try:
        return load_yaml(config_dir() / "analysis.yaml")
    except ConfigNotFound:
        return {}


def hypotheses_config() -> dict[str, Any]:
    """仮説の枠組み（層の掛け合わせ、歯科経営の定性要因、So What? のふるい）。

    ここに書いてあるのは統計ではなく**業界知識**です。データから出てくる
    ものではないので、モデルに思いつかせるより枠として与えたほうが確実で、
    扱う人が入れ替えるものなのでコードではなく設定に置きます。

    無い環境では空で返します。枠が無くても分析は成立します（質は落ちます）。
    """
    try:
        return load_yaml(config_dir() / "hypotheses.yaml")
    except ConfigNotFound:
        return {}


def prompt_text(name: str) -> str:
    """config/prompts/<name> をそのまま読む。

    プロンプトはコードではなく資料です。文字列リテラルとして Python の中に
    埋めると、差分が読めなくなり、非エンジニアが直せなくなります。
    """
    path = config_dir() / "prompts" / name
    if not path.is_file():
        raise ConfigNotFound(f"プロンプトが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


def insights_config() -> dict[str, Any]:
    """複合指標の定義。無くてもデータセットは組み立てられる。

    この 1 つだけ欠けても分析は成立するので、見つからないときは空で返します。
    設定ファイルの不在でデータセット全体が 500 になるのは割に合いません。
    """
    try:
        return load_yaml(config_dir() / "insights.yaml")
    except ConfigNotFound:
        return {}
