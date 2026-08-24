"""標榜診療科目の語彙。

医療情報ネットの 032 ファイルは (施設, 診療科目, 診療時間帯) の 1 行ずつで、
診療科目はコードと名称の両方を持ちます。ここではコードを正規化キーに写し、
そのキーに日本語の表示名を与えます。

コードで正規化するのは、名称に表記ゆれがあるためです（「歯科口腔外科」と
「口腔外科」、「障害者歯科」と「障がい者歯科」）。写像そのものは
``config/sources.yaml`` にあり、告示が科目を増やしたときにコードを直さずに
済むようにしてあります。

自由記載区分（08991）だけは別扱いです。インプラント・審美・訪問診療はこの
区分にしか現れず、しかも書いた医院しか数えられません。この区分から作られた
キーは :func:`is_free_text` が真を返し、API はそれを「宣言した医院数」として
表示します。競合数として使うと、書かなかった医院を「やっていない」と数えて
しまうためです。
"""
from __future__ import annotations

from typing import Any, Mapping

from kaigyou_core import config as cfg

#: `config/sources.yaml` のどのソースが語彙を持っているか。
SOURCE_ID = "mhlw_dental_specialties"

#: 正規化キーの日本語表示名。キーは英字、表示は日本語。UI と API が同じ名前を
#: 使うためにここに 1 か所だけ置いています。
LABELS: dict[str, str] = {
    "general": "一般歯科",
    "pediatric": "小児歯科",
    "orthodontics": "矯正歯科",
    "pediatric_orthodontics": "小児矯正歯科",
    "oral_surgery": "歯科口腔外科",
    "implant": "インプラント",
    "cosmetic": "審美・ホワイトニング",
    "home_visit": "訪問歯科診療",
    "periodontal": "歯周病",
    "preventive": "予防歯科",
    "special_needs": "障害者歯科",
    "dental_anesthesia": "歯科麻酔",
    "prosthodontics": "補綴・義歯",
    "endodontics": "歯内療法",
    "sleep_apnea": "睡眠時無呼吸",
    "other": "その他（歯科）",
    "other_medical": "歯科以外の標榜科",
}

#: 表示順。標榜科目が先、自由記載が後。LABELS に無いキーは末尾に回ります。
ORDER = (
    "general", "pediatric", "orthodontics", "pediatric_orthodontics", "oral_surgery",
    "implant", "cosmetic", "home_visit", "periodontal", "preventive",
    "special_needs", "prosthodontics", "endodontics", "dental_anesthesia",
    "sleep_apnea", "other", "other_medical",
)

#: 診療時間から導く印。値は API と UI が共有する表示名。
HOURS_LABELS: dict[str, str] = {
    "saturday": "土曜診療",
    "sunday": "日曜診療",
    "holiday": "祝日診療",
    "evening": "夜間診療",
}


def spec() -> Mapping[str, Any]:
    """語彙を持つソース定義。設定が無い環境では空扱い。"""
    return (cfg.sources_config().get("sources") or {}).get(SOURCE_ID) or {}


def code_map() -> dict[str, str]:
    """診療科目コード -> 正規化キー。"""
    return {str(k): str(v) for k, v in (spec().get("specialty_codes") or {}).items()}


def free_text_code() -> str:
    return str(spec().get("free_text_code") or "08991")


def non_dental_key() -> str:
    return str(spec().get("non_dental_key") or "other_medical")


def free_text_keywords() -> list[tuple[str, list[str]]]:
    """(キー, キーワード) の並び。長いキーワードから当てたいので順序を保つ。"""
    raw = spec().get("free_text_keywords") or {}
    return [(str(k), [str(v) for v in (vs if isinstance(vs, list) else [vs])])
            for k, vs in raw.items()]


def classify(code: str | None, name: str | None) -> tuple[str, bool]:
    """1 行の診療科目を (正規化キー, 自由記載か) に写す。

    コードが標榜科目のものならそれが答え。自由記載区分ならキーワードで当て、
    どれにも当たらなければ ``other``。歯科以外のコードは 1 つのキーにまとめ、
    歯科の競合数に混ざらないようにします。
    """
    code = (code or "").strip()
    name = (name or "").strip()
    codes = code_map()
    if code in codes:
        return codes[code], False
    if code == free_text_code():
        for key, words in free_text_keywords():
            if any(word and word in name for word in words):
                return key, True
        return "other", True
    # 08 以外は歯科以外の標榜科（内科・皮膚科など）。
    return non_dental_key(), False


def label(key: str) -> str:
    return LABELS.get(key, key)


def is_free_text(key: str) -> bool:
    """そのキーが自由記載区分からしか出てこないか。

    真なら、その件数は「その診療をしている医院の数」ではなく「そう書いた
    医院の数」です。数え落としの向きが一方向なので、競合の少なさの根拠には
    使えません。
    """
    if key in set(code_map().values()):
        return False
    return key != non_dental_key()


def sort_key(key: str) -> tuple[int, str]:
    try:
        return (ORDER.index(key), key)
    except ValueError:
        return (len(ORDER), key)


def describe(counts: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """件数の辞書を、表示順つき・表示名つき・自由記載の印つきの並びにする。"""
    if not counts:
        return []
    out = [
        {
            "key": key,
            "label": label(key),
            "count": int(value or 0),
            "declared_only": is_free_text(key),
        }
        for key, value in counts.items()
    ]
    out.sort(key=lambda row: sort_key(row["key"]))
    return out
