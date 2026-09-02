"""Configuration loading.

YAML files under ``config/`` are reloaded when their mtime changes, so editing
the scoring weights takes effect without restarting the API.

**設定は 2 段に分かれています。**

    config/sources.yaml     どの業態でも同じ（人口・事業所・駅・地価の出どころ）
    config/analysis.yaml    どの業態でも同じ（モデル・上限・段の構成）
    config/<業態>/…          その業態の知識（重み・複合指標・KSF・プロンプト）

分けているのは、業態の知識を**共通化できないから**です。「内科の開業で何を
答えるべきか」は歯科とは別の知識で、無理に 1 つの枠へまとめると両方に効かない
枠になります。共通化するのではなく、置き場所を分けて、業態で読み分けます。

フォルダの名前は ``facility_category`` そのものです（``config/dental_clinic/``）。
対応表を持たないので、ずれようがありません。

業態のフォルダに無いファイルは ``config/`` 直下を見ます。設定を移していない
環境をその場で壊さないためです。
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


def business_dir(category: str | None = None) -> Path:
    """その業態の設定フォルダ。名前は ``facility_category`` そのもの。"""
    from kaigyou_core.scoring import DEFAULT_FACILITY_CATEGORY

    return config_dir() / (category or DEFAULT_FACILITY_CATEGORY)


def business_file(name: str, category: str | None = None) -> Path:
    """業態のファイルを探し、無ければ ``config/`` 直下に落とす。

    落とし先を用意しているのは、設定を業態フォルダへ移していない環境
    （手元のコピー、`KAIGYOU_CONFIG_DIR` を差し替えた検証環境）を、この
    変更でその場で壊さないためです。**移行のためのものです。**
    """
    candidate = business_dir(category) / name
    return candidate if candidate.exists() else config_dir() / name


def scoring_config(category: str | None = None) -> dict[str, Any]:
    return load_yaml(business_file("scoring.yaml", category))


def sources_config() -> dict[str, Any]:
    """データの出どころ。**業態で分けません。**

    人口メッシュも駅も地価も、業態が変わっても同じファイルです。分けると
    同じ国勢調査の定義を業態の数だけ複製することになり、片方だけ直したときに
    「同じ商圏なのに業態で人口が違う」が起きます。業態ごとの施設ファイルは、
    ソースごとの ``facility_category`` で区別します。
    """
    return load_yaml(config_dir() / "sources.yaml")


def analysis_config() -> dict[str, Any]:
    """商圏インテリジェンス・エンジンの設定。無い環境では空。"""
    try:
        return load_yaml(config_dir() / "analysis.yaml")
    except ConfigNotFound:
        return {}


def hypotheses_config(category: str | None = None) -> dict[str, Any]:
    """仮説の枠組み（層の掛け合わせ、歯科経営の定性要因、So What? のふるい）。

    ここに書いてあるのは統計ではなく**業界知識**です。データから出てくる
    ものではないので、モデルに思いつかせるより枠として与えたほうが確実で、
    扱う人が入れ替えるものなのでコードではなく設定に置きます。

    無い環境では空で返します。枠が無くても分析は成立します（質は落ちます）。
    """
    try:
        return load_yaml(business_file("hypotheses.yaml", category))
    except ConfigNotFound:
        return {}


def prompt_text(name: str, category: str | None = None) -> str:
    """config/<業態>/prompts/<name> をそのまま読む。

    プロンプトはコードではなく資料です。文字列リテラルとして Python の中に
    埋めると、差分が読めなくなり、非エンジニアが直せなくなります。

    業態ごとに 1 式あります。段の構成（``analysis.yaml``）は共通で、同じ
    ファイル名の中身が業態ごとに違う、という形です。
    """
    path = business_file(str(Path("prompts") / name), category)
    if not path.is_file():
        raise ConfigNotFound(f"プロンプトが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


def dead_ends() -> list[dict[str, Any]]:
    """調べても答えの出ない問いの台帳（config/dead_ends.yaml）。

    **業態をまたいで共有します。** 「市区町村単位の医師の年齢構成が無い」は
    歯科でも医科でも同じ話で、業態ごとに書き分けると片方だけ古くなります。

    無くても分析は成立するので、見つからないときは空で返します。台帳が
    無いことで分析全体が落ちるのは割に合いません。
    """
    try:
        data = load_yaml(config_dir() / "dead_ends.yaml")
    except ConfigNotFound:
        return []
    return list(data.get("dead_ends") or [])


def insights_config(category: str | None = None) -> dict[str, Any]:
    """複合指標の定義。無くてもデータセットは組み立てられる。

    この 1 つだけ欠けても分析は成立するので、見つからないときは空で返します。
    設定ファイルの不在でデータセット全体が 500 になるのは割に合いません。
    """
    try:
        return load_yaml(business_file("insights.yaml", category))
    except ConfigNotFound:
        return {}


def positioning_config(category: str | None = None) -> dict[str, Any]:
    """地域の位置づけの軸・しきい値・地域タイプ（config/<業態>/positioning.yaml）。

    **重みをコードに書きません。** 変えるのに再デプロイが要り、変えた履歴が
    git log にしか残らなくなります。

    無くても分析は成立します（位置づけの節が出ないだけ）。設定ファイルの不在で
    データセット全体が 500 になるのは割に合いません。
    """
    try:
        return load_yaml(business_file("positioning.yaml", category))
    except ConfigNotFound:
        return {}


def city_planning_labels() -> dict[str, Any]:
    """都市計画の区分の「一言でいうと何か」。**業態に依りません。**

    用途地域の意味は診療所でも病院でも同じなので、業態フォルダの外にあります
    （そこに建てられるかどうかは :func:`city_planning_config` のほう）。
    地図の吹き出しとデータセットが同じ文を使うためで、片方だけ直すと画面と
    レポートで別のことを言い出します。
    """
    try:
        return load_yaml(config_dir() / "city_planning.yaml")
    except ConfigNotFound:
        return {}


def city_planning_config(category: str | None = None) -> dict[str, Any]:
    """用途地域・区域区分と、そこにこの業態の施設を建てられるかの規則。

    **業態で変わるのでコードに書きません。** 建築基準法 別表第2 では診療所は
    工業専用地域を除くすべての用途地域で建てられますが、病院は低層住居専用・
    第一種中高層住居専用・工業・工業専用で建てられません。同じ「医療施設」でも
    別の表になります。

    無ければ空。**空のときは可否を判定せず、区域名だけを出します**——
    規則が無いのに「建てられます」と書くほうが危険だからです。
    """
    try:
        return load_yaml(business_file("city_planning.yaml", category))
    except ConfigNotFound:
        return {}


def misreadings(category: str | None = None) -> list[dict[str, Any]]:
    """この画面の数字で、やってはいけない読み方。

    **注意書きの寄せ集めではなく、この製品の中身です。** jSTAT MAP も
    RESAS も TerraMap も正しい数字を出します。間違えるのは読む側で、
    ツールは黙って通します——だから誰も「不便だ」と言わず、気づかないまま
    自信を持ちます。

    実測：国勢調査の「在学者数」メッシュで西早稲田キャンパスを引くと 169 人
    でした。実際にそのキャンパスに通う学生は 3,000 人規模です。**18 倍**
    違います（在学者数は常住地基準で、「そこに通ってくる学生」ではない）。

    ``categories`` を持つ項目は、その業態のときだけ返します。歯科の標榜
    科目の話を内科の画面に出すと、そこだけ嘘になります。
    """
    try:
        raw = load_yaml(config_dir() / "misreadings.yaml").get("misreadings") or []
    except ConfigNotFound:
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        limited = item.get("categories")
        if limited and category is not None and category not in limited:
            continue
        out.append(dict(item))
    return out
