直前の分析を、指定された JSON スキーマに写してください。

**これは書き写す作業です。新しい主張を足さないでください。**

## 規則

- `reasoning` は **FACT → BENCHMARK → PATTERN → WHY → INSIGHT → IMPLICATION
  → ACTION の順**に並べてください。順番が推論の筋です。証拠が足りない段は
  `HYPOTHESIS` として WHY の位置に置きます。
- `segments` の `role` は `primary` / `secondary` / `avoid` のいずれか。
  `basis` には **2 つ以上のデータの組み合わせ**を書いてください。1 つだけなら、
  それは根拠として弱いので `caution` にその旨を書きます。
- `catchments` の `rank` は `primary` / `secondary`。**半径だけで引かないで
  ください。** 動線（駅・幹線道路・生活圏）で説明します。
- `opening_risks` と `before_opening` は 1 項目 1 文。
- `information_gaps` には、**競合について分からなかったこと**を書いてください。
  分からないことを「競合が少ない」と読み替えないこと。分かっていれば空で
  構いません。
- 本文で使った [FACT] などの印は、`reasoning` の `tag` に移してください。
  他の欄には印を残さないでください（読みにくくなります）。
