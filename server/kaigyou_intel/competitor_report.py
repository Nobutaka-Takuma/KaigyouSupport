"""競合分析のレポート（開発指示書 §4〜§6）。

周辺一般のレポートとは**別の文書**です。同じ関数に押し込まなかったのは、
載せるものが違うからではなく、**載せてはいけないものが違う**からです。
周辺一般のレポートは統計から地域の構造を説明します。この文書は競合 1 件ずつを
調べた結果で、統計の話は 1 行も出てきません。混ぜると、読み手には「どの数字が
何から来たのか」が分からなくなります。

書き出す順番には理由があります。

    1. 何を調べ、何を調べられなかったか   ← 先に言う
    2. 競争環境（LLM の要約）
    3. 何が多く、何が少ないか（Python の集計）
    4. ポジショニングマップ
    5. 機会仮説（**必ず但し書き付き**）

(1) が先なのは、この文書のいちばんの誤読が「1km 圏に 12 院」だからです。
上限で切った 12 件なのか、本当に 12 院しかないのかで、読み方が正反対に
なります。**結論のあとに書くと、読まれません。**

(5) の但し書きは省けません。「競合が少ない領域」は「まだ誰もやっていない」
ではなく「やってみて成立しなかった」かもしれず、この文書のデータでは
どちらとも言えません。指示書 §6 が禁じているのはそこです。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from kaigyou_intel.report import _sources_block, _table

#: 免責。周辺一般のレポートと同じ文言で、**この文書の分だけ**足します。
#: 「競合が少ない」から売上や患者数を推すことは、このデータではできません。
DISCLAIMER = (
    "この文書は、公開情報（各事業者の Web サイト等）から確認できた内容の集計です。"
    "開業の成功、収益性、患者数等を示すものではありません。"
    "各事業者が実際に扱っている内容は、Web に書かれていない部分を含みます。"
)


def to_markdown(output: Mapping[str, Any], dataset: Mapping[str, Any],
                sources: Sequence[Mapping[str, Any]] = ()) -> str:
    """競合分析 1 本ぶんの Markdown。"""
    location = dataset.get("location") or {}
    place = " ".join(x for x in (location.get("prefecture_name"),
                                 location.get("municipality_name")) if x)
    name = location.get("name") or place or \
        f"{location.get('lat')},{location.get('lng')}"
    tally = output.get("tally") or {}
    coverage = output.get("coverage") or {}
    label = _label(output)

    lines = [f"# {name} 周辺の競合分析", ""]
    lines += [f"地点 {location.get('lat')}, {location.get('lng')}"
              f" / 半径 {_radius(coverage, dataset)} / 対象 {label}", ""]

    lines += _coverage_block(coverage, tally, label)
    lines += _landscape_block(output, tally, label)
    lines += _tally_block(tally, label)
    lines += _map_block(output.get("positioning_map") or {}, label)
    lines += _opportunity_block(output, label)
    lines += _unconfirmed_block(output)
    lines += _sources_block(sources)
    lines += ["## 免責", "", DISCLAIMER, ""]
    return "\n".join(lines).rstrip() + "\n"


def _label(output: Mapping[str, Any]) -> str:
    return str(output.get("label") or "競合")


def _radius(coverage: Mapping[str, Any], dataset: Mapping[str, Any]) -> str:
    radius = coverage.get("radius_m") or (dataset.get("query") or {}).get("radius_m")
    return f"{int(radius):,}m" if radius else "—"


def _coverage_block(coverage: Mapping[str, Any], tally: Mapping[str, Any],
                    label: str) -> list[str]:
    """**何を調べ、何を調べられなかったか。**

    この節を先頭に置くのは、以降の件数がすべて「調べた範囲の中の件数」
    だからです。上限で切った件数を、その地域に存在する件数として読まれると、
    この文書の数字は全部ずれます。
    """
    surveyed = tally.get("surveyed") or coverage.get("surveyed") or 0
    total = coverage.get("total_in_radius")
    not_surveyed = int(coverage.get("not_surveyed") or 0)
    failed = list(coverage.get("failed") or [])

    lines = ["## この分析で調べた範囲", ""]
    if total is not None:
        lines.append(f"- 半径内の{label}：**{int(total):,} 件**"
                     f"（施設データベースより）")
    lines.append(f"- Web で調べた：**{int(surveyed):,} 件**")
    near = tally.get("within_near")
    if near is not None and tally.get("near_radius_m"):
        lines.append(f"- うち {int(tally['near_radius_m']):,}m 圏："
                     f"**{int(near):,} 件**")
    if not_surveyed:
        lines.append(
            f"- 調べていない：**{not_surveyed:,} 件**"
            "（近い順に上限で切りました。**その地域に存在しない"
            "という意味ではありません**）")
    if failed:
        lines.append(f"- 調べたが構造化できなかった：**{len(failed):,} 件**")
        lines += [f"    - {f.get('name')}：{f.get('why')}" for f in failed]
    lines += ["",
              "以降の件数は、すべて**この調べた範囲の中の件数**です。"
              f"各{label}の Web サイトに書かれていない項目は数に入りません——"
              "**扱っていないという意味ではありません。**", ""]
    return lines


def _landscape_block(output: Mapping[str, Any], tally: Mapping[str, Any],
                     label: str) -> list[str]:
    """競争環境（指示書 §6）。**ここだけが LLM の文章です。**"""
    landscape = str(output.get("landscape") or "").strip()
    character = str(output.get("character") or "").strip()
    if not (landscape or character):
        return []
    lines = ["## 競争環境", ""]
    if character:
        lines += [f"**{character}**", ""]
    if landscape:
        lines += [landscape, ""]
    for key, heading in (("crowded", "競争が集中している領域"),
                         ("sparse", f"比較的{label}が少ない領域")):
        items = [str(x) for x in (output.get(key) or []) if str(x).strip()]
        if items:
            lines += [f"### {heading}", ""] + [f"- {x}" for x in items] + [""]
    return lines


def _tally_block(tally: Mapping[str, Any], label: str) -> list[str]:
    """何が多く、何が少ないか（指示書 §4）。**Python が数えた値です。**

    0 件の行を消しません。「この地域にインプラントを掲げる医院は無い」は、
    行が無いことでは伝わりません——**調べ落としと区別が付かない**からです。
    """
    if not tally:
        return []
    lines = ["## この地域では何が多く、何が少ないか", ""]
    for key, heading in (("products", "取り扱っている領域"),
                         ("targets", "訴求している顧客層"),
                         ("place", "立地・営業条件")):
        rows = [[r.get("label") or r.get("key"), f"{int(r.get('count') or 0)} 件",
                 "設定の語彙外" if r.get("outside_vocabulary") else ""]
                for r in (tally.get(key) or [])]
        if rows:
            lines += [f"### {heading}", ""]
            lines += _table(["項目", f"{label}数", ""], rows)

    positioning = tally.get("positioning") or []
    if positioning:
        lines += [f"### 各{label}が掲げている強み（自由記述）", ""]
        lines += _table(["訴求", f"{label}数"],
                        [[r.get("label"), f"{int(r.get('count') or 0)} 件"]
                         for r in positioning])

    leaning = tally.get("leaning_x_high")
    leaning_label = tally.get("leaning_x_high_label") or ""
    if leaning is not None and leaning_label:
        lines += [f"{leaning_label}寄りと判定した{label}：**{int(leaning):,} 件**", ""]
    if tally.get("note"):
        lines += [str(tally["note"]), ""]
    return lines


def _map_block(pmap: Mapping[str, Any], label: str) -> list[str]:
    """ポジショニングマップ（指示書 §5）。

    **判定困難を地図に載せません。** 載せると、他の点と同じ確かさに見えます。
    置けなかった件は、理由とともに表の外に出します。
    """
    placed = list(pmap.get("placed") or [])
    undecided = list(pmap.get("undecided") or [])
    if not (placed or undecided):
        return []
    x, y = pmap.get("x") or {}, pmap.get("y") or {}
    lines = ["## ポジショニングマップ", ""]
    if x or y:
        lines += [f"- 横軸：{x.get('low', '−')} ←→ {x.get('high', '＋')}"
                  f"（{x.get('label', '')}）",
                  f"- 縦軸：{y.get('low', '−')} ←→ {y.get('high', '＋')}"
                  f"（{y.get('label', '')}）", ""]

    quadrants = [q for q in (pmap.get("quadrants") or [])]
    if quadrants:
        lines += _table(["区画", f"{label}数"],
                        [[q.get("label"), f"{int(q.get('count') or 0)} 件"]
                         for q in quadrants])
        # **空いている区画を「機会」と読ませない。** そう読めてしまうのが
        # この図のいちばんの危うさで、図だけでは区別が付きません。
        lines += ["0 件の区画は、**そこに機会があるという意味ではありません。**"
                  "やってみて成立しなかった可能性も、この図では区別できません。",
                  ""]

    if placed:
        lines += [f"### 各{label}の位置", ""]
        lines += _table([label, "横", "縦", "距離", "判定の根拠"],
                        [[p.get("name"), _signed(p.get("x")), _signed(p.get("y")),
                          _metres(p.get("distance_m")), p.get("basis") or "—"]
                         for p in sorted(placed, key=_by_distance)])
    if undecided:
        lines += [f"### 位置を判定できなかった{label}"
                  f"（{len(undecided)} 件）", ""]
        lines += [f"- {u.get('name')}：{u.get('why')}" for u in undecided] + [""]
    if pmap.get("note"):
        lines += [str(pmap["note"]), ""]
    return lines


def _opportunity_block(output: Mapping[str, Any], label: str) -> list[str]:
    """機会仮説（指示書 §6）。**但し書きの無い仮説は出しません。**

    「競合が少ない＝市場機会がある」は、この文書のデータからは言えません。
    但し書きを任意にすると、書かれない回が出ます。書かれなかった回だけが
    断定に見えるので、**無い場合はこちらで補います。**
    """
    items = [h for h in (output.get("opportunities") or []) if h.get("position")]
    if not items:
        return []
    lines = ["## 機会仮説（**仮説であって、結論ではありません**）", ""]
    for h in items:
        lines.append(f"### {h.get('position')}")
        lines.append("")
        if h.get("why"):
            lines += [f"- そう言える根拠：{h['why']}"]
        lines += [f"- **外れるとしたら**：{h.get('caveat') or _DEFAULT_CAVEAT}", ""]
    lines += [f"{label}が少ないことは、**そこに需要があることを意味しません。**"
              "少ない理由が「まだ誰もやっていない」なのか「やってみて"
              "成立しなかった」なのかは、この分析では区別できません。", ""]
    return lines


_DEFAULT_CAVEAT = ("この領域の競合が少ないのは、需要が無いからかもしれません。"
                   "この分析では区別できません。")


def _unconfirmed_block(output: Mapping[str, Any]) -> list[str]:
    items = [str(x) for x in (output.get("not_determinable") or []) if str(x).strip()]
    if not items:
        return []
    return (["## 調べたが確認できなかったこと", ""]
            + [f"- {x}" for x in items] + [""])


def _by_distance(point: Mapping[str, Any]) -> float:
    value = point.get("distance_m")
    return float("inf") if value is None else float(value)


def _signed(value: Any) -> str:
    return "—" if value is None else f"{int(value):+d}"


def _metres(value: Any) -> str:
    return "—" if value is None else f"{int(float(value)):,}m"
