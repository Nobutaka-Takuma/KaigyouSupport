"""テスト全体の前提。

**節約設定を切って走らせます。**

`config/analysis.yaml` の `budget.mode` は、動作確認のあいだ思考を浅く・検索を
少なく・競合を数件に絞るための一時的な措置です。テストが見たいのはそちらでは
なく、**本来の設定**——「判断の段は effort を削らない」「2 周調べる」といった、
この製品が守ると決めたことのほうです。効いている値を見てしまうと、節約設定を
入れた日にそれらのテストが一斉に赤くなり、赤いのは設計が壊れたからなのか
節約中だからなのかが区別できません。

節約設定そのものは test_competitors.py が、環境変数を明示的に立てて見ます。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _full_strength_config():
    """このセッションでは節約設定を無効にする（空文字 = 節約しない）。"""
    before = os.environ.get("KAIGYOU_BUDGET_MODE")
    os.environ["KAIGYOU_BUDGET_MODE"] = ""
    yield
    if before is None:
        os.environ.pop("KAIGYOU_BUDGET_MODE", None)
    else:
        os.environ["KAIGYOU_BUDGET_MODE"] = before
