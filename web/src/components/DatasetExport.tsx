import { useState } from "react";
import { ApiError, api } from "../lib/api";

/**
 * 表示中の地点の全データをJSONで取り出すボタン。
 *
 * 用途はLLMへの貼り付けと、他のツールへの持ち出し。だからコピーが主で、
 * ダウンロードが従。クリップボードはブラウザやhttp接続で塞がれることが
 * あるので、失敗したら黙って諦めずファイル保存に落とす。
 *
 * バイト数を出しているのは、貼り付ける前に「どれくらいの量か」が分かる
 * ようにするため。医院一覧の有無で1万バイト以上変わる。
 */
export function DatasetExport({
  lat, lng, radius, profile, catchment,
}: {
  lat: number;
  lng: number;
  radius: number;
  profile?: string;
  catchment?: "circle" | "walk";
}) {
  const [state, setState] = useState<"idle" | "loading" | "copied" | "saved" | "error">("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [size, setSize] = useState<number | null>(null);
  const [withClinics, setWithClinics] = useState(true);

  async function fetchJson(pretty: boolean): Promise<string> {
    const data = await api.dataset({
      lat, lng, radius,
      catchment,
      profile: profile || undefined,
      max_clinics: withClinics ? 50 : 0,
    });
    // 貼り付け先は機械なので詰めて出す。改行とインデントで倍近くなり、
    // それはそのままLLMのトークンになる。保存するファイルのほうは人が
    // エディタで開くので整形する。
    const text = pretty ? JSON.stringify(data, null, 2) : JSON.stringify(data);
    setSize(new Blob([text]).size);
    return text;
  }

  function fileName() {
    return `kaigyou-${lat.toFixed(5)}-${lng.toFixed(5)}-${radius}m.json`;
  }

  function save(text: string) {
    const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = fileName();
    link.click();
    URL.revokeObjectURL(url);
  }

  async function onCopy() {
    setState("loading");
    setMessage(null);
    try {
      const text = await fetchJson(false);
      try {
        await navigator.clipboard.writeText(text);
        setState("copied");
      } catch {
        // クリップボードが使えない環境（http、権限拒否など）。
        // 何も起きないより、ファイルとして渡すほうがまし。
        save(text);
        setState("saved");
        setMessage("クリップボードが使えないため、ファイルとして保存しました。");
      }
    } catch (e) {
      setState("error");
      setMessage(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function onDownload() {
    setState("loading");
    setMessage(null);
    try {
      save(await fetchJson(true));
      setState("saved");
    } catch (e) {
      setState("error");
      setMessage(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <div className="dataset-export">
      <h3>データ出力（JSON）</h3>
      <p className="muted small">
        この地点について取得できるデータをすべて1つのJSONにまとめて出力します。
        単位・定義・出典・注意事項も同梱されるので、そのままLLMに貼り付けられます。
      </p>
      <div className="dataset-export__actions">
        <button type="button" onClick={onCopy} disabled={state === "loading"}>
          {state === "loading" ? "取得中…" : "JSONをコピー"}
        </button>
        <button type="button" className="secondary" onClick={onDownload}
                disabled={state === "loading"}>
          ダウンロード
        </button>
        <label className="dataset-export__opt">
          <input
            type="checkbox"
            checked={withClinics}
            onChange={(e) => { setWithClinics(e.target.checked); setState("idle"); }}
          />
          歯科医院の一覧を含める
        </label>
      </div>
      {state === "copied" && (
        <p className="muted small">
          コピーしました{size != null && `（${(size / 1024).toFixed(1)} KB）`}。
        </p>
      )}
      {state === "saved" && (
        <p className="muted small">
          {message ?? `保存しました（${fileName()}）`}
          {size != null && ` ${(size / 1024).toFixed(1)} KB`}
        </p>
      )}
      {state === "error" && <p className="warn-inline">取得に失敗しました: {message}</p>}
    </div>
  );
}
