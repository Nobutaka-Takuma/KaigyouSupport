"""商圏インテリジェンス・エンジン。

/api/dataset が出す基礎 JSON を入力に、4 ステップの推論を経て 1 本のレポートに
するオーケストレーション。要件定義書 COMMERCIAL_AREA_INTELLIGENCE_REQUIREMENTS.md
に対応します。

構造:

    projection.py   ステップごとに、渡す断面を切り出す（渡さなければ言えない）
    schemas.py      各ステップの出力の形と、参照が解決するかの検算
    client.py       Anthropic API を呼ぶ唯一の場所
    jobs.py         Job / Step の状態。DB が持つ（worker は記憶を持たない）
    steps/          4 ステップの実装
    worker.py       キューを回す
"""
