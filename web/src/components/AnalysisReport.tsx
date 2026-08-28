import { useState } from "react";
import { api } from "../lib/api";
import type {
  AnalysisReport, AnalysisSource, ClientReportJson, Evidenced, ReportBlock,
  ReportJson,
} from "../lib/types";

/**
 * レポートを構造のまま表示する。
 *
 * Markdown を貼るのではなく report_json を描くのは、根拠の id（F001, C002…）を
 * 見える形で残すためです。§25 が求めているのは「INSIGHT から FACT、そして
 * 出典まで辿れること」で、それは本文を流し込んだだけでは満たせません。
 * Markdown は持ち出し用として、そのまま落とせるようにしてあります。
 */

const TAG_LABEL: Record<ReportBlock["tag"], string> = {
  FACT: "事実", BENCHMARK: "比較", PATTERN: "パターン", WHY: "背景",
  INSIGHT: "解釈", IMPLICATION: "示唆", ACTION: "行動",
};

const DECISION_FIELDS: [string, keyof ReportJson["decision"]][] = [
  ["主要患者", "primary_patients"],
  ["主要に置かない層", "secondary_patients"],
  ["競争しない領域", "avoid_competing_on"],
  ["患者獲得エリア", "acquisition_area"],
  ["来院理由", "reason_to_visit"],
  ["医院モデル", "clinic_model"],
];

/** 要件 §9 の優先順位。並び順もこの順。 */
const SOURCE_TYPES = [
  ["government", "国・省庁"], ["statistics", "政府統計"], ["prefecture", "都道府県"],
  ["municipality", "市区町村"], ["public_body", "公的機関"], ["transit", "交通事業者"],
  ["academic", "大学・研究機関"], ["company", "企業"], ["news", "ニュース"],
  ["other", "その他"],
] as const;

const SOURCE_LABEL: Record<string, string> =
  Object.fromEntries(SOURCE_TYPES.map(([key, label]) => [key, label]));

/** 上から読んで一次資料に当たれるように並べる。Markdown 側と同じ順。 */
function byPriority(a: AnalysisSource, b: AnalysisSource) {
  const rank = (s: AnalysisSource) => {
    const i = SOURCE_TYPES.findIndex(([key]) => key === (s.source_type ?? "other"));
    return i < 0 ? SOURCE_TYPES.length : i;
  };
  return rank(a) - rank(b) || (a.url ?? "").localeCompare(b.url ?? "");
}

export function AnalysisReportView(
  { report, jobId }: { report: AnalysisReport; jobId: string },
) {
  if (isClientReport(report.report_json)) {
    return <ClientReportView report={report} json={report.report_json} jobId={jobId} />;
  }
  return <WorkingReportView report={report} json={report.report_json} jobId={jobId} />;
}

/** 顧客提出用の形で保存されたレポートか。段の構成を変える前の古い
 *  ジョブは、タグ付きの働き用の形のまま残っています。 */
function isClientReport(json: ClientReportJson | ReportJson): json is ClientReportJson {
  return "verdict" in json && "support_needed" in json;
}

/**
 * 顧客に渡す文書。散文で、結論から。
 *
 * タグ（FACT / PATTERN …）は出しません。あれは検算のための形で、読み物の形では
 * ありません。根拠の id だけは各段落の末尾に小さく残します。§25 の追跡は
 * そこを通るので、読みやすさのために消すわけにいきません。
 */
function ClientReportView(
  { report, json, jobId }:
    { report: AnalysisReport; json: ClientReportJson; jobId: string },
) {
  const [open, setOpen] = useState<"report" | "support" | "sources">("report");
  const cited = list(report.sources).filter((s) => s.pattern_id).sort(byPriority);
  const support = list(json.support_needed);
  const research = list(json.further_research);
  const sections = list(json.sections);
  type Support = NonNullable<ClientReportJson["support_needed"]>;
  const byCategory = new Map<string, Support>();
  for (const item of support) {
    byCategory.set(item.category, [...(byCategory.get(item.category) ?? []), item]);
  }

  return (
    <div className="report">
      <div className="report__summary">
        <strong className="report__verdict">{json.verdict.label}</strong>
        {json.summary}
      </div>

      <div className="report__tabs" role="tablist">
        {([["report", "レポート"], ["support", `開業に必要なこと (${support.length})`],
           ["sources", `出典 (${cited.length})`]] as const).map(([key, label]) => (
          <button key={key} role="tab" aria-selected={open === key}
                  className={open === key ? "is-active" : ""}
                  onClick={() => setOpen(key)}>
            {label}
          </button>
        ))}
        {report.report_markdown && (
          <button className="report__download" onClick={() => { void download(jobId); }}>
            Markdownで保存
          </button>
        )}
      </div>

      {open === "report" && (
        <div className="report__prose">
          <h4>評価：{json.verdict.label}</h4>
          <p>{json.verdict.statement}<Refs ids={json.verdict.basis} /></p>
          {json.verdict.counterpoint && (
            <p className="report__counterpoint">
              <strong>この判断が外れるとしたら</strong>　{json.verdict.counterpoint}
            </p>
          )}

          <h4>なぜこの立地か</h4>
          <p>{json.why_here}</p>

          {sections.map((section, i) => (
            <section key={i}>
              <h4>{section.heading}</h4>
              {section.takeaway && (
                <p className="report__takeaway">{section.takeaway}</p>
              )}
              <p>{section.body}<Refs ids={section.evidence} /></p>
            </section>
          ))}

          {research.length > 0 && (
            <>
              <h4>さらに深掘りすべき調査</h4>
              <p className="report__note">
                本レポートは公的統計から読み取れる範囲です。ここから先は現地・
                一次情報の領域で、調査の方向としては次が考えられます。
              </p>
              {research.map((item, i) => (
                <div key={i} className="report__support">
                  <strong>{item.topic}</strong>
                  <p>{item.why}</p>
                  <p className="report__how">調べ方: {item.how}</p>
                </div>
              ))}
            </>
          )}

          <details className="report__legend">
            <summary>文中の〔F001〕などについて</summary>
            <p>
              文末の〔　〕は、その記述の根拠にした項目の番号です。どの記述が
              どのデータに基づくかを後から辿れるように付けています。
            </p>
            <ul>
              {Object.entries(EVIDENCE_LEGEND).map(([prefix, label]) => (
                <li key={prefix}><code>{prefix}001</code> … {label}</li>
              ))}
            </ul>
          </details>
          <p className="report__judgement-note">{json.judgement_note}</p>
        </div>
      )}

      {open === "support" && (
        <div className="report__prose">
          {[...byCategory].map(([category, items]) => (
            <section key={category}>
              <h4>{category}</h4>
              {items.map((item, i) => (
                <div key={i} className="report__support">
                  <strong>{item.item}</strong>
                  <p>{item.why}<Refs ids={item.evidence} /></p>
                </div>
              ))}
            </section>
          ))}
        </div>
      )}

      {open === "sources" && <SourceList report={report} cited={cited} />}
      <p className="report__disclaimer">{report.disclaimer}</p>
    </div>
  );
}

function SourceList(
  { report, cited }: { report: AnalysisReport; cited: AnalysisSource[] },
) {
  return (
    <div className="report__sources">
      {cited.length === 0 && <p>本文が引用した外部資料はありません。</p>}
      <ul>
        {cited.map((source) => (
          <li key={`${source.pattern_id}-${source.url}`}>
            <span className="report__source-type">
              {SOURCE_LABEL[source.source_type ?? "other"] ?? "その他"}
            </span>
            <a href={source.url} target="_blank" rel="noreferrer noopener">
              {shorten(source.title ?? source.url)}
            </a>
          </li>
        ))}
      </ul>
      {list(report.sources).length > cited.length && (
        <p className="report__source-note">
          このほか {list(report.sources).length - cited.length} 件を参照しましたが、
          本文の根拠としては引用していません。
        </p>
      )}
    </div>
  );
}

function WorkingReportView(
  { report, json, jobId }:
    { report: AnalysisReport; json: ReportJson; jobId: string },
) {
  const [open, setOpen] = useState<"decision" | "body" | "sources">("decision");
  const cited = list(report.sources).filter((s) => s.pattern_id).sort(byPriority);

  return (
    <div className="report">
      <div className="report__summary">{json.executive_summary}</div>

      <div className="report__tabs" role="tablist">
        {([["decision", "開業方針"], ["body", "レポート本文"],
           ["sources", `出典 (${cited.length})`]] as const).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={open === key}
            className={open === key ? "is-active" : ""}
            onClick={() => setOpen(key)}
          >
            {label}
          </button>
        ))}
        {report.report_markdown && (
          <button className="report__download" onClick={() => { void download(jobId); }}>
            Markdownで保存
          </button>
        )}
      </div>

      {open === "decision" && (
        <div className="report__decision">
          <dl>
            {DECISION_FIELDS.map(([label, key]) => (
              <div key={String(key)}>
                <dt>{label}</dt>
                <dd><Statement item={json.decision[key] as Evidenced} /></dd>
              </div>
            ))}
          </dl>
          <EvidencedList title="開業上のメリット" items={list(json.decision.advantages)} />
          <EvidencedList title="リスク" items={list(json.decision.risks)} />
          <EvidencedList title="次に取るべき行動" items={list(json.actions)} />
          <p className="report__confidence">
            判断の確度: <strong>{json.decision.confidence}</strong>
          </p>
        </div>
      )}

      {open === "body" && (
        <div className="report__body">
          {[...list(json.sections)].sort((a, b) => a.number - b.number).map((section) => (
            <section key={section.number}>
              <h4>{section.number}. {section.title}</h4>
              {list(section.blocks).map((block, i) => (
                <p key={i} className={`report__block report__block--${block.tag}`}>
                  <span className="report__tag">{TAG_LABEL[block.tag] ?? block.tag}</span>
                  {block.text}
                  <Refs ids={block.evidence} />
                </p>
              ))}
            </section>
          ))}
        </div>
      )}

      {open === "sources" && <SourceList report={report} cited={cited} />}

      <p className="report__disclaimer">{report.disclaimer}</p>
    </div>
  );
}

function Statement({ item }: { item: Evidenced | undefined }) {
  if (!item) return null;
  return <>{item.statement}<Refs ids={item.evidence} /></>;
}

function EvidencedList({ title, items }: { title: string; items: Evidenced[] }) {
  if (!items?.length) return null;
  return (
    <>
      <h4>{title}</h4>
      <ul>
        {items.map((item, i) => (
          <li key={i}><Statement item={item} /></li>
        ))}
      </ul>
    </>
  );
}

/** 記号の読み方。説明の無い記号は、読み手にとっては模様と同じです。 */
const EVIDENCE_LEGEND: Record<string, string> = {
  F: "基礎データから読み取った事実",
  P: "複数の事実から見えた特徴",
  C: "外部資料で確認した事実",
  H: "背景についての仮説と判定",
  S: "推定した患者層",
  M: "需要が生まれる筋道",
  I: "横断して見えたこと",
};


/** 根拠の id。ここが §25 の追跡の入り口なので、本文から消さない。 */
function Refs({ ids }: { ids?: string[] }) {
  if (!Array.isArray(ids) || ids.length === 0) return null;
  return (
    <span className="report__refs">
      {ids.map((id) => (
        <code key={id} title={EVIDENCE_LEGEND[id[0]] ?? "根拠の番号"}>{id}</code>
      ))}
    </span>
  );
}

/**
 * 保存済みの JSON は、いまのコードと同じ形とは限りません。
 *
 * レポートは DB に入ったまま何か月も残ります。その間にスキーマは変わります。
 * 実測：`questions_for_the_client` を `further_research` に直したとき、
 * 前の形で保存されたレポートを開いた画面が
 * `Cannot read properties of undefined (reading 'length')` で真っ白になりました。
 *
 * **古い形でも読めること**を、レポートを見せる側の責任にします。
 */
function list<T>(value: T[] | undefined | null): T[] {
  return Array.isArray(value) ? value : [];
}


/** e-Stat の表題は481文字あった。一覧で読めなくなる。 */
function shorten(text: string, limit = 80) {
  const head = text.split(" | ")[0].trim() || text;
  return head.length > limit ? `${head.slice(0, limit - 1)}…` : head;
}

/**
 * サーバの ``/report.md`` を通して保存します。
 *
 * ここで Blob を組み立てていたころは、名前が「商圏分析レポート.md」で
 * 固定でした。マイレポートからの保存はサーバが名前を付けるので、**同じ
 * 文書が入口によって別の名前で手元に残ります。** 名前を決める場所は
 * 1 か所にします。
 */
async function download(jobId: string) {
  await api.analysis.download(jobId);
}
