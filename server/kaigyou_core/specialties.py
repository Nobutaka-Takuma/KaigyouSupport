"""標榜診療科目の語彙。

医療情報ネットの 032 ファイルは (施設, 診療科目, 診療時間帯) の 1 行ずつで、
診療科目はコードと名称の両方を持ちます。ここではコードを正規化キーに写し、
そのキーに日本語の表示名を与えます。

コードで正規化するのは、名称に表記ゆれがあるためです（「歯科口腔外科」と
「口腔外科」、「障害者歯科」と「障がい者歯科」）。

**語彙はすべて設定にあります**（``config/sources.yaml``）。以前はコード表だけが
YAML にあり、表示名と並び順は Python の dict でした。告示が科目を増やすたびに
2 か所を直すことになり、片方だけ直すと「キーはあるのに表示名が英字のまま」に
なります。医科の科目は数十あるので、その状態では追えません。

**どの語彙かは業態が決めます。** 語彙を持つソースは ``specialty_codes`` を
持つソースで、そのソースの ``facility_category`` がどの業態のものかを言います。
歯科と医科では科目の体系そのものが別なので、1 つの表に混ぜることはできません。

自由記載区分（歯科では 08991）だけは別扱いです。インプラント・審美・訪問診療は
この区分にしか現れず、しかも書いた医院しか数えられません。この区分から作られた
キーは :func:`is_free_text` が真を返し、API はそれを「宣言した医院数」として
表示します。競合数として使うと、書かなかった医院を「やっていない」と数えて
しまうためです。
"""
from __future__ import annotations

from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_core.scoring import DEFAULT_FACILITY_CATEGORY

#: 歯科の語彙を持つソース。**業態から引けなかったときの落とし先**です。
#: 通常は ``facility_category`` で解決するので、ここは使われません。
SOURCE_ID = "mhlw_dental_specialties"


def spec(category: str = DEFAULT_FACILITY_CATEGORY) -> Mapping[str, Any]:
    """その業態の語彙を持つソース定義。設定が無い環境では空扱い。

    ``specialty_codes`` を持つソースが語彙の持ち主で、その ``facility_category``
    がどの業態のものかを言います。見つからなければ空を返します——**別の業態の
    語彙で代用しません。** 歯科の科目名で内科を分類したものは、間違っていても
    それらしく見えます。
    """
    sources = cfg.sources_config().get("sources") or {}
    for source in sources.values():
        if (source.get("specialty_codes")
                and source.get("facility_category") == category):
            return source
    # 業態が書かれていない古い設定のための落とし先。歯科のときだけ効きます。
    if category == DEFAULT_FACILITY_CATEGORY:
        return sources.get(SOURCE_ID) or {}
    return {}


def code_map(category: str = DEFAULT_FACILITY_CATEGORY) -> dict[str, str]:
    """診療科目コード -> 正規化キー。"""
    return {str(k): str(v)
            for k, v in (spec(category).get("specialty_codes") or {}).items()}


def labels(category: str = DEFAULT_FACILITY_CATEGORY) -> dict[str, str]:
    """正規化キー -> 日本語の表示名。UI と API が同じ名前を使うための 1 か所。"""
    return {str(k): str(v)
            for k, v in (spec(category).get("specialty_labels") or {}).items()}


def order(category: str = DEFAULT_FACILITY_CATEGORY) -> tuple[str, ...]:
    """表示順。標榜科目が先、自由記載が後。ここに無いキーは末尾に回ります。"""
    return tuple(str(x) for x in (spec(category).get("specialty_order") or []))


def hours_labels(category: str = DEFAULT_FACILITY_CATEGORY) -> dict[str, str]:
    """診療時間から導く印の表示名（土曜診療・夜間診療など）。"""
    return {str(k): str(v)
            for k, v in (spec(category).get("hours_labels") or {}).items()}


def free_text_code(category: str = DEFAULT_FACILITY_CATEGORY) -> str:
    return str(spec(category).get("free_text_code") or "08991")


def non_dental_key(category: str = DEFAULT_FACILITY_CATEGORY) -> str:
    """その語彙に含まれない標榜科をまとめるキー。"""
    return str(spec(category).get("non_dental_key") or "other_medical")


def free_text_keywords(
        category: str = DEFAULT_FACILITY_CATEGORY) -> list[tuple[str, list[str]]]:
    """(キー, キーワード) の並び。長いキーワードから当てたいので順序を保つ。"""
    raw = spec(category).get("free_text_keywords") or {}
    return [(str(k), [str(v) for v in (vs if isinstance(vs, list) else [vs])])
            for k, vs in raw.items()]


def classify(code: str | None, name: str | None,
             category: str = DEFAULT_FACILITY_CATEGORY) -> tuple[str, bool]:
    """1 行の診療科目を (正規化キー, 自由記載か) に写す。

    コードが標榜科目のものならそれが答え。自由記載区分ならキーワードで当て、
    どれにも当たらなければ ``other``。その語彙に無いコードは 1 つのキーに
    まとめ、競合数に混ざらないようにします。
    """
    code = (code or "").strip()
    name = (name or "").strip()
    codes = code_map(category)
    if code in codes:
        return codes[code], False
    if code == free_text_code(category):
        for key, words in free_text_keywords(category):
            if any(word and word in name for word in words):
                return key, True
        return "other", True
    # 08 以外は歯科以外の標榜科（内科・皮膚科など）。
    return non_dental_key(category), False


def label(key: str, category: str = DEFAULT_FACILITY_CATEGORY) -> str:
    return labels(category).get(key, key)


def is_free_text(key: str, category: str = DEFAULT_FACILITY_CATEGORY) -> bool:
    """そのキーが自由記載区分からしか出てこないか。

    真なら、その件数は「その診療をしている医院の数」ではなく「そう書いた
    医院の数」です。数え落としの向きが一方向なので、競合の少なさの根拠には
    使えません。
    """
    if key in set(code_map(category).values()):
        return False
    return key != non_dental_key(category)


def sort_key(key: str, category: str = DEFAULT_FACILITY_CATEGORY) -> tuple[int, str]:
    sequence = order(category)
    try:
        return (sequence.index(key), key)
    except ValueError:
        return (len(sequence), key)


def describe(counts: Mapping[str, Any] | None,
             category: str = DEFAULT_FACILITY_CATEGORY) -> list[dict[str, Any]]:
    """件数の辞書を、表示順つき・表示名つき・自由記載の印つきの並びにする。"""
    if not counts:
        return []
    out = [
        {
            "key": key,
            "label": label(key, category),
            "count": int(value or 0),
            "declared_only": is_free_text(key, category),
        }
        for key, value in counts.items()
    ]
    out.sort(key=lambda row: sort_key(row["key"], category))
    return out
