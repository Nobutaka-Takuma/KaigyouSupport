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
from typing import Any, Mapping

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
    """商圏インテリジェンス・エンジンの設定。無い環境では空。

    ``budget.mode`` が立っていれば、その節約設定を**上から重ねて**返します。
    読む側は今までどおり `analysis_config()` を読むだけで、節約中かどうかを
    気にしなくて済みます。
    """
    try:
        return _with_budget(load_yaml(config_dir() / "analysis.yaml"))
    except ConfigNotFound:
        return {}


#: 節約設定が上書きしてよい `limits` の項目。
#:
#: **白紙委任にはしません。** budget の下に何を書いても効く作りにすると、
#: 打ち間違えた項目が黙って無視され、節約したつもりで満額請求されます。
_BUDGET_LIMITS = frozenset({
    "max_patterns", "searches_per_pattern", "max_searches_total",
    "parallel_research", "max_clinics_in_projection", "clinics_to_research",
    "surroundings_searches", "research_rounds", "followup_searches",
})

#: `limits` 以外に節約設定が効く項目。
#:
#:   effort / max_tokens  … 段ごとの指定ごと外して、モデル既定を差し替える
#:   web_search           … 段の検索を落とす
#:   competitors          … 競合分析の survey に重ねる（competitors_config）
_BUDGET_OTHER = frozenset({"effort", "max_tokens", "web_search", "competitors"})

#: 節約設定として認識する項目のすべて。**ここに無い項目は効きません。**
BUDGET_KEYS = _BUDGET_LIMITS | _BUDGET_OTHER


class UnknownBudgetKey(ValueError):
    """節約設定に、効かない項目が書かれている。

    黙って無視すると、節約したつもりで満額請求されます。打ち間違いは
    起動時に気づけるほうがよい。
    """


def _with_budget(config: dict[str, Any]) -> dict[str, Any]:
    """節約設定を重ねる（`budget.mode` が指す節）。

    **段ごとの設定を書き換えるのではなく、上から重ねます。** 書き換えると、
    元の値がどこにも残らず「元に戻す」が手作業になります。mode を消せば
    元の設定がそのまま効きます。
    """
    budget = config.get("budget") or {}
    mode = _budget_mode_name(budget)
    profile = (budget.get(mode) or {}) if mode else {}
    if not profile:
        return config
    unknown = sorted(set(profile) - BUDGET_KEYS)
    if unknown:
        raise UnknownBudgetKey(
            f"config/analysis.yaml の budget.{mode} に、効かない項目があります: "
            f"{', '.join(unknown)}。使えるのは {', '.join(sorted(BUDGET_KEYS))} です。")

    out = dict(config)
    out["limits"] = {**(config.get("limits") or {}),
                     **{k: v for k, v in profile.items() if k in _BUDGET_LIMITS}}
    # effort と max_tokens は段ごとに書いてあります。**段の指定ごと消します**
    # ——段に effort: high と書いてあると、モデル既定を下げても効きません。
    for key in ("steps", "competitor_steps"):
        steps = config.get(key) or {}
        out[key] = {n: _capped(step, profile) for n, step in steps.items()}
    out["model"] = {**(config.get("model") or {}),
                    **{k: profile[k] for k in ("effort", "max_tokens")
                       if k in profile}}
    return out


def _capped(step: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """1 段ぶんの設定から、節約中に無視したい指定を外す。"""
    out = dict(step)
    for key in ("effort", "effort_structure", "effort_scan"):
        if "effort" in profile:
            out.pop(key, None)
    if "max_tokens" in profile:
        out.pop("max_tokens", None)
    # 検索を切るなら、段の web_search も落とします。**残すと、上限 0 回の
    # 検索ツールを渡すことになります**（モデルは呼ぼうとして失敗します）。
    if profile.get("web_search") is False:
        out["web_search"] = False
    return out


def budget_mode() -> str | None:
    """いま効いている節約設定の名前。効いていなければ None。"""
    try:
        budget = load_yaml(config_dir() / "analysis.yaml").get("budget") or {}
    except ConfigNotFound:
        return None
    mode = _budget_mode_name(budget)
    return mode if mode and budget.get(mode) else None


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


#: 商圏半径の既定。**config/<業態>/insights.yaml の catchment に無いときだけ。**
#:
#: 500m にしています。1km の円は 3.14km² で、駅から 800m 離れた候補地を中心に
#: 置いても駅も駅前商店街も飲み込みます。歯科医院のような小規模事業では、
#: 出てくるのが「その一帯の分析」になり、**候補地を 300m 動かしても同じ結論**
#: が出ます。
FALLBACK_RADIUS_M = 500


def default_radius_m(category: str | None = None) -> int:
    """その業態の既定の商圏半径。

    **業態ごとに違います。** 歯科医院は徒歩とママチャリで来る範囲が主戦場
    ですが、総合病院や大型店は桁が違います。コードに定数を置きません。
    """
    catchment = (insights_config(category).get("catchment") or {})
    try:
        return int(catchment.get("default_radius_m") or FALLBACK_RADIUS_M)
    except (TypeError, ValueError):
        return FALLBACK_RADIUS_M


def competitors_config(category: str | None = None) -> dict[str, Any]:
    """競合分析の語彙と予算（config/<業態>/competitors.yaml）。

    **枠（STP と 4P）は業態を問わず同じで、中に入る語だけが違います。**
    歯科なら「インプラント」、飲食なら「客単価」、学習塾なら「集団／個別」。
    枠はスキーマに、語はここに。新しい業態は設定を足すだけで始められます。
    """
    try:
        conf = load_yaml(business_file("competitors.yaml", category))
    except ConfigNotFound:
        return {}
    # 節約設定は survey の上に重ねます。**この設定ファイルは書き換えません**
    # ——書き換えると、budget.mode を消しても元に戻らなくなります。
    saving = (_budget_profile() or {}).get("competitors") or {}
    if not saving:
        return conf
    return {**conf, "survey": {**(conf.get("survey") or {}), **saving}}


def _budget_profile() -> dict[str, Any]:
    """いま効いている節約設定の中身。効いていなければ空。"""
    try:
        budget = load_yaml(config_dir() / "analysis.yaml").get("budget") or {}
    except ConfigNotFound:
        return {}
    mode = _budget_mode_name(budget)
    return (budget.get(mode) or {}) if mode else {}


def _budget_mode_name(budget: Mapping[str, Any]) -> str | None:
    """どの節約設定を使うか。**環境変数がファイルより強い。**

    設定ファイルを書き換えずに切り替えられる口が要ります。本番と手元で
    違う設定にしたいときや、テストが本来の設定を見たいときに、ファイルを
    編集させるとコミットに混ざります。空文字は「節約しない」です。
    """
    override = os.getenv("KAIGYOU_BUDGET_MODE")
    if override is not None:
        return override.strip() or None
    return budget.get("mode") or None


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
