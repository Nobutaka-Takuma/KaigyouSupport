/**
 * この画面の数字で、やってはいけない読み方。
 *
 * **注意書きの寄せ集めではなく、この製品の中身です。**
 *
 * jSTAT MAP も RESAS も TerraMap も、正しい数字を出します。間違えるのは
 * 読む側で、ツールは黙って通します——だから誰も「不便だ」と言わず、
 * 気づかないまま自信を持ちます。実測：国勢調査の「在学者数」メッシュで
 * 西早稲田キャンパスを引くと 169 人でした。実際にそのキャンパスに通う学生は
 * 3,000 人規模です（在学者数は常住地基準で、通ってくる学生ではない）。
 *
 * **文はサーバから来ます。** 画面が独自に書くと、地図で見た注意とレポートの
 * 注意が食い違い、どちらが正しいのか読み手には分かりません。
 */
import { useEffect, useState } from "react";

import { api } from "../lib/api";
import type { Misreading } from "../lib/types";

/** 最初から開いておく件数。**畳んだままだと読まれません。** */
const OPEN_AT_FIRST = 3;

export function Misreadings({ category }: { category?: string }) {
  const [items, setItems] = useState<Misreading[]>([]);
  const [note, setNote] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [opened, setOpened] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .misreadings({ category })
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setNote(res.note);
      })
      // 取れなくても地図は使えます。ここで画面を止めません。
      .catch(() => !cancelled && setItems([]));
    return () => {
      cancelled = true;
    };
  }, [category]);

  if (!items.length) return null;

  // 判断がひっくり返るものを先に。全部を常時出すと 15 項目になり、
  // **長い注意書きは読まれません。**
  const high = items.filter((m) => m.severity === "high");
  const shown = expanded ? items : high.slice(0, OPEN_AT_FIRST);
  const hidden = items.length - shown.length;

  return (
    <section className="misread">
      <h3 className="misread__title">この数字で、やってはいけない読み方</h3>
      <p className="misread__note">{note}</p>
      <ul className="misread__list">
        {shown.map((m) => (
          <li key={m.id} className={`misread__item is-${m.severity}`}>
            <button
              type="button"
              className="misread__trap"
              aria-expanded={opened === m.id}
              onClick={() => setOpened(opened === m.id ? null : m.id)}
            >
              {m.trap}
            </button>
            {opened === m.id && (
              <div className="misread__body">
                <p><strong>なぜ</strong>　{m.why}</p>
                <p><strong>ではどう読むか</strong>　{m.instead}</p>
              </div>
            )}
          </li>
        ))}
      </ul>
      {hidden > 0 && (
        <button type="button" className="linklike"
                onClick={() => setExpanded(true)}>
          ほか {hidden} 件を表示
        </button>
      )}
      {expanded && (
        <button type="button" className="linklike" onClick={() => setExpanded(false)}>
          畳む
        </button>
      )}
    </section>
  );
}
