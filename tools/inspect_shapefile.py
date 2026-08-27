"""zip の中の shapefile が、どんな属性を持っているかを見る。

取り込む前に**実物を見る**ためのものです。国土数値情報のメッシュ系は
推計の版で属性名が変わります（PTN_2025 / POP2025 …）。設定が実物と
合っていないと、静かに 0 件になるか、別の列を人口として読みます。
後者のほうが怖い。数字は出るのに間違っているからです。

    python tools/inspect_shapefile.py 500m_mesh_2024_22_SHP.zip
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from kaigyou_etl.adapters._util import read_shapefile  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    member = sys.argv[2] if len(sys.argv) > 2 else None
    fields, records = read_shapefile(path, member_prefix=member)

    print(f"{path.name}: {len(records):,} 件 / 属性 {len(fields)} 個\n")
    print("属性名:")
    for name in fields:
        print(f"  {name}")

    if records:
        print("\n先頭 1 件の中身:")
        for name, value in zip(fields, records[0].record):
            print(f"  {name:<16} = {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
