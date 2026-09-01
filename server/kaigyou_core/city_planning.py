"""都市計画の区分名を、設定と突き合わせるための 1 か所。

**公表データの区分名は表記がゆれます。** 静岡県の A55 は全角数字で
「第１種低層住居専用地域」と書きますが、漢数字で「第一種低層住居専用地域」と
書く県もあります。人が読むぶんには同じですが、辞書の鍵としては別物です。

これを各所で個別に処理すると、片方だけ直った状態になります。実際そうなり
ました——``config/dental_clinic/city_planning.yaml`` を漢数字で書いたため、
静岡県では **第１種低層住居専用地域の規模制限が一度も出ていませんでした。**
しかも「建てられます」と表示されるので、間違いが目に見えません
（工業専用地域と市街化調整区域には数字が無いので、そちらだけ効いていました）。

だから、設定を引くのは必ずこのモジュール経由にします。
"""
from __future__ import annotations

from typing import Any, Mapping

from kaigyou_core import config as cfg

#: 全角数字・算用数字 -> 漢数字。用途地域の名前に出てくるのは 1〜2 だけですが、
#: 3 まで入れておきます（「第三種」を作る告示があっても落ちないように）。
_DIGITS = str.maketrans({
    "１": "一", "２": "二", "３": "三",
    "1": "一", "2": "二", "3": "三",
})


def canonical(zone: str | None) -> str:
    """区分名を、設定の鍵と突き合わせられる形にする。

    >>> canonical("第１種低層住居専用地域")
    '第一種低層住居専用地域'
    >>> canonical("第一種低層住居専用地域")
    '第一種低層住居専用地域'
    """
    return (zone or "").strip().translate(_DIGITS)


def describe(zone: str | None) -> str | None:
    """その区分が一言でいうと何か。業態に依りません。

    知らない区分には説明を付けません（``None``）。**でっち上げるより空欄です。**
    """
    descriptions = cfg.city_planning_labels().get("descriptions") or {}
    return descriptions.get(canonical(zone))


def rule_for(zone: str | None, category: str | None = None,
             rules: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """その区分に、この業態の施設を建てられるかの規則。無ければ ``None``。

    用途地域の規則と区域区分の規則を 1 つの引き方にまとめています。呼ぶ側が
    「これは用途地域だから zone_rules」と判断すると、区域区分を渡されたときに
    黙って規則なしになります。

    ``rules`` を渡せば設定の読み直しを省けます（1 リクエストで何百件も引く
    地図レイヤのため）。
    """
    if rules is None:
        rules = cfg.city_planning_config(category)
    key = canonical(zone)
    found = (rules.get("zone_rules") or {}).get(key)
    if found is None:
        found = (rules.get("area_division_rules") or {}).get(key)
    return dict(found) if found else None
