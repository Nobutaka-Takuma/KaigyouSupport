import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../lib/api";
import { authConfigured, storedSession } from "../lib/auth";
import type { AnalysisReport, AnalysisStatus, AnalysisStep } from "../lib/types";
import { AnalysisReportView } from "./AnalysisReport";

/**
 * 商圏インテリジェンス（4ステップのLLM分析）を、この地点で走らせる。
 *
 * 分析はここでは走りません。APIはJobを作るだけで、実行はワークステーション側の
 * workerです。だからこのパネルの仕事は「始める」「待っているものを見せる」
 * 「終わったレポートを見せる」の3つで、いちばん大事なのは2番目です。
 * workerが動いていないときに、ぐるぐる回るだけの画面にしないこと。
 *
 * 進捗はポーリングで取ります。WebSocketにしないのは、Vercelの関数では
 * 保てないからで、これは実行時間の上限と同じ制約です。
 */

/**
 * この失敗は、下の「分析トークン」欄の話か。
 *
 * 401 が返る理由は2通りあり、直し方が正反対です。
 *
 *   アカウントを使わない環境 → 共有シークレット（この欄）が要る
 *   アカウントを使う環境     → サインインの問題。この欄は見られもしない
 *
 * 以前は 401 と 503 なら何でも欄を出していたので、署名方式やアカウント権限の
 * エラーが「分析トークン」の下に出て、利用者は無関係な欄を疑うことに
 * なっていました。実際にそれで手が止まりました。
 *
 * 判定は状態コードではなくサーバの文言で行います。どちらの入口を使うかを
 * 決めているのはサーバ（intel.py の create_analysis）で、こちらの設定から
 * 推測すると、client と server の設定がずれたときに黙って食い違います。
 * これらの語を出すのは _authorise の2つの失敗だけです。
 */
function aboutTheToken(err: ApiError): boolean {
  return /X-Analysis-Token|KAIGYOU_ANALYSIS_TOKEN/.test(err.message);
}

/**
 * サーバの言い分に、利用者が次にする一手を足す。
 *
 * 「ログインが必要です」は正しいが、サインイン欄は画面のいちばん上にあって
 * ここからは見えません。どこを押せばいいかまで言わないと、正しい文言でも
 * 迷わせます。
 */
function explain(err: ApiError): string {
  if (!authConfigured()) return err.message;
  // 共有シークレットの話なら、サーバの文言をそのまま。ここで「サインインを」と
  // 言い換えると、押す場所のない案内になります（サーバはトークンで守っていて、
  // サインインしても通りません）。client と server の設定がずれた環境で起きます。
  if (aboutTheToken(err)) return err.message;
  if (err.status === 401 && !storedSession()) {
    return "分析を始めるにはサインインが必要です。画面右上からサインインしてください。";
  }
  return err.message;
}

const TOKEN_KEY = "kaigyou.analysisToken";
const JOB_KEY = "kaigyou.lastJob";

const STATUS_LABEL: Record<string, string> = {
  pending: "待機", running: "実行中", completed: "完了",
  failed: "失敗", skipped: "スキップ",
};

export function AnalysisPanel({
  lat, lng, radius, profile, catchment, locationName,
}: {
  lat: number;
  lng: number;
  radius: number;
  profile?: string;
  catchment?: "circle" | "walk";
  locationName?: string;
}) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<AnalysisStatus | null>(null);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? "");
  const [needsToken, setNeedsToken] = useState(false);
  // ポーリングは終わった時点で止まります。やり直したら再開させる必要があるので、
  // 動かすたびに増やす数を持ちます。
  const [attempt, setAttempt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const timer = useRef<number | null>(null);

  // 直前のジョブは覚えておきます。分析は数分かかるので、その間にタブを
  // 閉じたり地点を見直したりするのは普通のことです。
  //
  // ただし**同じ地点のときだけ**戻します。地点を持たずに戻すと、裾野を見て
  // いる画面に銀座のレポートが出ます。それは間違いというより、気づけない
  // 間違いです。
  useEffect(() => {
    setStatus(null);
    setReport(null);
    setError(null);
    const saved = localStorage.getItem(JOB_KEY);
    if (!saved) {
      setJobId(null);
      return;
    }
    try {
      const at = JSON.parse(saved) as
        { id: string; lat: number; lng: number; radius: number };
      setJobId(samePlace(at, { lat, lng, radius }) ? at.id : null);
    } catch {
      setJobId(null);
    }
  }, [lat, lng, radius]);

  const poll = useCallback(async (id: string) => {
    try {
      const next = await api.analysis.status(id);
      setStatus(next);
      if (next.report_available) {
        setReport(await api.analysis.report(id));
      }
      return next;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      return null;
    }
  }, []);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;

    const tick = async () => {
      const next = await poll(jobId);
      if (cancelled) return;
      // 終わったジョブを叩き続けない。queued のままでも間隔は空ける
      // （worker が起きていないときに毎秒問い合わせても意味がない）。
      const done = next && ["completed", "cancelled", "failed", "blocked"]
        .includes(next.job.status);
      if (!done) timer.current = window.setTimeout(tick, 4000);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [jobId, poll, attempt]);

  // 待っている間、何も動かない画面にしない。1つのステップに数分かかるので、
  // 止まっているのか進んでいるのかが分からなくなります。
  const waiting = status
    && ["queued", "running"].includes(status.job.status);
  // 経過を秒で出しているので、running のあいだは毎秒描き直します。
  useEffect(() => {
    if (!waiting) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [waiting]);

  async function start() {
    setStarting(true);
    setError(null);
    setReport(null);
    setStatus(null);
    try {
      const created = await api.analysis.create(
        { lat, lng, radius, catchment, profile: profile || undefined,
          location_name: locationName || undefined },
        token || undefined);
      localStorage.setItem(JOB_KEY,
        JSON.stringify({ id: created.job_id, lat, lng, radius }));
      if (token) localStorage.setItem(TOKEN_KEY, token);
      setJobId(created.job_id);
      setAttempt((n) => n + 1);
      setNeedsToken(false);
    } catch (err) {
      if (err instanceof ApiError && aboutTheToken(err)) setNeedsToken(true);
      setError(err instanceof ApiError ? explain(err) : String(err));
    } finally {
      setStarting(false);
    }
  }

  /**
   * 失敗したところからやり直す。
   *
   * ボタンをもう一度押すと**新しいジョブ**ができます。それは「別の分析を
   * 始める」であって「さっきの続き」ではありません。済んだステップまで
   * 捨てて課金し直すことになるので、失敗しているときは retry を出します。
   */
  async function retry(from: number) {
    if (!jobId) return;
    setError(null);
    try {
      await api.analysis.retryFrom(jobId, from, token || undefined);
      setAttempt((n) => n + 1);
    } catch (err) {
      setError(err instanceof ApiError ? explain(err) : String(err));
    }
  }

  const steps = status?.steps ?? [];
  const done = steps.filter((s) => s.status === "completed").length;
  const failedStep = steps.find((s) => s.status === "failed");
  const failed = status?.job.status === "failed";
  const runningStep = steps.find((s) => s.status === "running");
  const queued = status?.job.status === "queued";
  // 数えはじめる時刻は「いま動いているステップが始まった時刻」。ジョブの作成
  // 時刻から数えると、worker を止めていた時間まで入ります（実測：worker を
  // 起動し直した直後に「経過 34分17秒」と出ました）。
  // 応答が途絶えている疑い。ホスティングされた関数には実行時間の上限があり
  // （Hobby 300秒）、それを超えて生きているステップは存在し得ません。にも
  // かかわらず経過時間だけが増え続けると、動いているように見えます。実測：
  // 5分で死んだ STEP4 の経過が15分まで伸びるのを、動作中として表示しました。
  // 待ち行列に戻るまでは数分かかるので、そのことを言います。
  const STALLED_AFTER_SEC = 330;
  const stalledFor = runningStep?.started_at
    ? (now - new Date(runningStep.started_at).getTime()) / 1000
    : 0;
  const stalled = stalledFor > STALLED_AFTER_SEC;

  const elapsed = queued
    ? null
    : since(runningStep?.started_at ?? status?.job.started_at, now);

  return (
    <section className="analysis">
      <div className="analysis__head">
        <h3>商圏インテリジェンス</h3>
        {failed && failedStep ? (
          <span className="analysis__actions">
            <button onClick={() => retry(failedStep.step_number)}>
              STEP{failedStep.step_number} からやり直す
            </button>
            <button className="analysis__ghost" onClick={() => retry(1)}>
              最初から
            </button>
          </span>
        ) : (
          <button onClick={start} disabled={starting || waiting === true}>
            {starting ? "開始中…"
              : queued ? "順番待ち…"
              : waiting ? "実行中…"
              : "この地点で分析を開始"}
          </button>
        )}
      </div>
      <p className="analysis__lead">
        統計から特徴を抽出し（STEP1）、その背景をWeb検索で調べ（STEP2）、
        需要が生まれる筋道を組み立て（STEP3）、開業方針に変換します（STEP4）。
        数分かかり、1件あたり1ドル前後のAPI費用が発生します。
      </p>

      {/* サーバが要求したときだけ。保存済みの値があっても、アカウント運用の
          画面では出しません（使われないので、あるだけ誤解を招く）。 */}
      {(needsToken || (token && !authConfigured())) && (
        <label className="analysis__token">
          <span>分析トークン</span>
          <input
            type="password"
            value={token}
            placeholder="X-Analysis-Token"
            onChange={(e) => setToken(e.target.value)}
          />
          <small>
            課金を伴う操作なので、公開URLでは共有シークレットを要求します。
            この端末にだけ保存されます。
          </small>
        </label>
      )}

      {error && <p className="analysis__error">{error}</p>}

      {status && (
        <>
          <p className="analysis__status">
            <strong>{done} / {steps.length}</strong> ステップ完了
            {runningStep && <> — STEP{runningStep.step_number} 実行中</>}
            {elapsed && <> {elapsed}</>}
            {status.status_note && <> — {status.status_note}</>}
          </p>

          {stalled && (
            <p className="warn-inline">
              STEP{runningStep?.step_number} からの応答が
              {Math.floor(stalledFor / 60)}分以上ありません。実行が途中で
              終わった可能性があります。数分のうちに自動でやり直します
              （このまま待っていて構いません）。
            </p>
          )}

          {failedStep?.error_message && <FailureNote text={failedStep.error_message} />}

          {/* 終わったジョブに「キーがありません」と出しても意味がありません。
              これは待っている人へのヒントです。 */}
          {status.llm_configured === false && !["completed", "cancelled"]
            .includes(status.job.status) && (
            <p className="analysis__warn">
              サーバ側で ANTHROPIC_API_KEY が確認できません。worker を動かす端末で
              設定してください。
            </p>
          )}

          <ol className="analysis__steps">
            {steps.map((step) => (
              <StepRow key={step.step_number} step={step} now={now} />
            ))}
          </ol>

          <p className="analysis__usage">
            入力 {status.usage.input_tokens.toLocaleString()} tok ／
            出力 {status.usage.output_tokens.toLocaleString()} tok ／
            Web検索 {status.usage.web_searches} 回
            {status.usage.estimated_cost_usd !== null && (
              <> ／ 概算 ${status.usage.estimated_cost_usd.toFixed(2)}</>
            )}
          </p>

          {status.trace_ok === false && (
            <p className="analysis__warn">
              根拠の追跡に解決できない参照が残っています。レポートの引用元を
              確認してください。
            </p>
          )}
        </>
      )}

      {report && <AnalysisReportView report={report} />}
    </section>
  );
}

/** 「1分23秒」。止まっているのか進んでいるのかが分かればよいので、秒まで。 */
function since(iso: string | null | undefined, now: number): string | null {
  if (!iso) return null;
  const started = Date.parse(iso);
  if (Number.isNaN(started)) return null;
  const seconds = Math.max(0, Math.floor((now - started) / 1000));
  return seconds < 60 ? `${seconds}秒` : `${Math.floor(seconds / 60)}分${seconds % 60}秒`;
}


/** 地図のクリックは毎回わずかに違う座標になるので、丸めて比べます。 */
function samePlace(
  a: { lat: number; lng: number; radius: number },
  b: { lat: number; lng: number; radius: number },
) {
  return a.radius === b.radius
    && Math.abs(a.lat - b.lat) < 1e-5
    && Math.abs(a.lng - b.lng) < 1e-5;
}


/**
 * 失敗の理由を、読める形にして必要なら手当てまで書く。
 *
 * 保存されている本文にはスタックトレースが付いています。画面にそのまま
 * 流すと、いちばん大事な1行が埋もれます。
 */
function FailureNote({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const first = text.split("\n")[0];
  const advice = adviceFor(text);
  return (
    <div className="analysis__failure">
      <p>{first}</p>
      {advice && <p className="analysis__advice">{advice}</p>}
      <button className="analysis__ghost" onClick={() => setOpen(!open)}>
        {open ? "詳細を隠す" : "詳細"}
      </button>
      {open && <pre>{text}</pre>}
    </div>
  );
}

/** よくある失敗には、次にやることまで書く。「失敗しました」だけでは動けない。 */
function adviceFor(text: string): string | null {
  if (text.includes("credit balance is too low")) {
    return "Anthropic の残高不足です。console.anthropic.com の Plans & Billing で"
      + "クレジットを追加してから、やり直してください。課金は発生していません。";
  }
  if (text.includes("rate_limit") || text.includes("429")) {
    return "レート制限です。数分おいてからやり直してください。";
  }
  if (text.includes("authentication") || text.includes("invalid x-api-key")) {
    return "ANTHROPIC_API_KEY が正しくありません。worker を動かす端末で"
      + "設定し直してから、worker を再起動してください。";
  }
  if (text.includes("overloaded") || text.includes("529")) {
    return "Anthropic 側が混雑しています。数分おいてからやり直してください。"
      + "課金は発生していません。";
  }
  if (text.includes("max_tokens") || text.includes("EOF while parsing")) {
    return "レポートが書き終わる前に長さの上限で切れました。"
      + "config/analysis.yaml の max_tokens を上げて worker を再起動し、"
      + "そのステップだけやり直してください。済んだステップは残ります。";
  }
  if (text.includes("検索結果に無い URL")) {
    return "モデルが実在しない出典を書いたため保存しませんでした。"
      + "やり直すと通ることがあります。";
  }
  return null;
}


function StepRow({ step, now }: { step: AnalysisStep; now: number }) {
  // キャッシュに入ったぶんは input_tokens から抜けます。足さないと、
  // 合計と行の数字が合いません。
  const cost = (step.input_tokens ?? 0) + (step.output_tokens ?? 0)
    + (step.cache_read_tokens ?? 0) + (step.cache_write_tokens ?? 0);
  return (
    <li className={`analysis__step analysis__step--${step.status}`}>
      <span className="analysis__step-name">
        STEP{step.step_number} {step.step_name}
      </span>
      <span className="analysis__step-status">
        {STATUS_LABEL[step.status] ?? step.status}
      </span>
      {step.status === "completed" && cost > 0 && (
        <small>{cost.toLocaleString()} tok</small>
      )}
      {step.status === "running" && step.started_at && (
        <small>{since(step.started_at, now) ?? ""}</small>
      )}
      {/* やり直しは進行中の出来事であって、失敗ではありません。赤くしない。 */}
      {(step.attempts ?? 0) > 0 && step.status !== "failed" && (
        <small className="analysis__retry">やり直し {step.attempts}回</small>
      )}
      {step.status === "failed" && (
        <small className="analysis__step-error">
          {(step.attempts ?? 0) > 0
            ? `${step.attempts}回やり直しましたが通りませんでした`
            : "このステップから再開できます"}
        </small>
      )}
    </li>
  );
}
