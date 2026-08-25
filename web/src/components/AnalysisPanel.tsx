import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../lib/api";
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
  }, [jobId, poll]);

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
      setNeedsToken(false);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 503)) {
        setNeedsToken(true);
      }
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  }

  const steps = status?.steps ?? [];
  const done = steps.filter((s) => s.status === "completed").length;

  return (
    <section className="analysis">
      <div className="analysis__head">
        <h3>商圏インテリジェンス</h3>
        <button onClick={start} disabled={starting}>
          {starting ? "開始中…" : "この地点で分析を開始"}
        </button>
      </div>
      <p className="analysis__lead">
        統計から特徴を抽出し（STEP1）、その背景をWeb検索で調べ（STEP2）、
        需要が生まれる筋道を組み立て（STEP3）、開業方針に変換します（STEP4）。
        数分かかり、1件あたり1ドル前後のAPI費用が発生します。
      </p>

      {(needsToken || token) && (
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
            {status.status_note && <> — {status.status_note}</>}
          </p>

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
            {steps.map((step) => <StepRow key={step.step_number} step={step} />)}
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

/** 地図のクリックは毎回わずかに違う座標になるので、丸めて比べます。 */
function samePlace(
  a: { lat: number; lng: number; radius: number },
  b: { lat: number; lng: number; radius: number },
) {
  return a.radius === b.radius
    && Math.abs(a.lat - b.lat) < 1e-5
    && Math.abs(a.lng - b.lng) < 1e-5;
}


function StepRow({ step }: { step: AnalysisStep }) {
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
      {step.error_message && (
        <small className="analysis__step-error">{step.error_message}</small>
      )}
    </li>
  );
}
