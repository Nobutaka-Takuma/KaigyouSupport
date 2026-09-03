import { useState } from "react";
import { api } from "../lib/api";
import { renderMarkdown } from "../lib/markdown";
import type {
  AnalysisReport, AnalysisSource, ClientReportJson, CompetitorReportJson,
  CompetitorTally, CountedLabel, Evidenced, PositioningMap, ReportBlock,
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
  // **競合分析はまったく別の文書です。** 統計の話は 1 行も出てきません。
  // 同じ描画に通すと、空の見出しが並んだレポートが出来上がります——
  // 落ちないので、成功したように見えます。
  if (isCompetitorReport(report.report_json)) {
    return <CompetitorReportView report={report} json={report.report_json}
                                 jobId={jobId} />;
  }
  if (isClientReport(report.report_json)) {
    return <ClientReportView report={report} json={report.report_json} jobId={jobId} />;
  }
  return <WorkingReportView report={report} json={report.report_json} jobId={jobId} />;
}

function isCompetitorReport(
  json: ClientReportJson | ReportJson | CompetitorReportJson,
): json is CompetitorReportJson {
  return "character" in json && "landscape" in json;
}

/**
 * 提出用の文書を、画面のまま読めるようにする。
 *
 * これまでは「Markdownで保存」しかなく、落として別のアプリで開く必要が
 * ありました。**提出する文書を確かめるのに、毎回アプリを行き来する必要は
 * ありません。**
 *
 * 隣の「レポート」タブとは別物です。あちらは根拠の id（F001 など）を辿る
 * ための構造の表示で、こちらは**相手に渡すものそのもの**です。
 */
function MarkdownView({ markdown, jobId }: { markdown: string; jobId: string }) {
  return (
    <div className="report__markdown">
      <p className="report__markdown-note">
        歯科医師に渡す文書そのものです。
        <button type="button" className="linklike"
                onClick={() => { void download(jobId); }}>
          Markdownで保存
        </button>
        すると、そのままファイルになります。
      </p>
      {/* renderMarkdown はすべてエスケープしてから組み立てるので、ここに
          渡してよい文字列です（詳しくは lib/markdown.ts）。 */}
      <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(markdown) }} />
    </div>
  );
}


/** 顧客提出用の形で保存されたレポートか。段の構成を変える前の古い
 *  ジョブは、タグ付きの働き用の形のまま残っています。 */
function isClientReport(
  json: ClientReportJson | ReportJson | CompetitorReportJson,
): json is ClientReportJson {
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
  const [open, setOpen] =
    useState<"report" | "support" | "sources" | "markdown">("report");
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
           ["sources", `出典 (${cited.length})`],
           // **提出する文書そのもの。** これまでは保存して別のアプリで開く
           // しかありませんでした。
           ...(report.report_markdown
               ? ([["markdown", "提出用の文書"]] as const) : []),
          ] as const).map(([key, label]) => (
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

      {open === "markdown" && report.report_markdown && (
        <MarkdownView markdown={report.report_markdown} jobId={jobId} />
      )}

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
  const [open, setOpen] =
    useState<"decision" | "body" | "sources" | "markdown">("decision");
  const cited = list(report.sources).filter((s) => s.pattern_id).sort(byPriority);

  return (
    <div className="report">
      <div className="report__summary">{json.executive_summary}</div>

      <div className="report__tabs" role="tablist">
        {([["decision", "開業方針"], ["body", "レポート本文"],
           ["sources", `出典 (${cited.length})`],
           ...(report.report_markdown
               ? ([["markdown", "提出用の文書"]] as const) : []),
          ] as const).map(([key, label]) => (
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

      {open === "markdown" && report.report_markdown && (
        <MarkdownView markdown={report.report_markdown} jobId={jobId} />
      )}

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


/**
 * 競合分析のレポート（開発指示書 §4〜§6）。
 *
 * **どこまでが数えた値で、どこからが解釈かを、画面の上で分けます。**
 * 集計とポジショニングマップは Python が数えました。LLM が書いたのは
 * 競争環境の文と機会仮説だけです。混ぜて並べると、読み手にはどちらも
 * 同じ確かさに見えます。
 *
 * 最初のタブが「調べた範囲」なのは、この文書のいちばんの誤読が
 * 「1km 圏に 12 院」だからです。上限で切った 12 件なのか、本当に 12 院なのかで
 * 読み方が正反対になります。
 */
function CompetitorReportView(
  { report, json, jobId }:
    { report: AnalysisReport; json: CompetitorReportJson; jobId: string },
) {
  const [open, setOpen] =
    useState<"landscape" | "tally" | "map" | "sources" | "markdown">("landscape");
  const cited = list(report.sources).sort(byPriority);
  const tally = json.tally;
  const pmap = json.positioning_map;
  const coverage = json.coverage ?? {};
  const label = json.label ?? "競合";
  const opportunities = list(json.opportunities);

  return (
    <div className="report">
      <div className="report__summary">
        <strong className="report__verdict">競合分析</strong>
        {json.character}
      </div>

      {/* **調べた範囲を、タブの外に出します。** タブの中に入れると、
          開かなければ見えません。以降の件数は全部この範囲の中の件数です。 */}
      <div className="report__coverage">
        <strong>この分析で調べた範囲</strong>
        <ul>
          {coverage.total_in_radius != null && (
            <li>
              半径{coverage.radius_m ? `${coverage.radius_m.toLocaleString()}m` : ""}
              内の{label}：{coverage.total_in_radius} 件（施設データベースより）
            </li>
          )}
          <li>Web で調べた：<strong>{tally?.surveyed ?? coverage.surveyed ?? 0} 件</strong></li>
          {tally?.near_radius_m != null && (
            <li>
              うち {tally.near_radius_m.toLocaleString()}m 圏：
              <strong>{tally.within_near} 件</strong>
            </li>
          )}
          {!!coverage.not_surveyed && (
            <li className="report__caveat">
              上限で切って調べていない：{coverage.not_surveyed} 件　
              <strong>その地域に存在しないという意味ではありません。</strong>
            </li>
          )}
          {list(coverage.failed).length > 0 && (
            <li>
              調べたが構造化できなかった：{list(coverage.failed).length} 件
              （{list(coverage.failed).map((f) => f.name).join("、")}）
            </li>
          )}
        </ul>
        <p className="report__note">
          以降の件数は、すべてこの範囲の中の件数です。各{label}の Web サイトに
          書かれていない項目は数に入りません——
          <strong>扱っていないという意味ではありません。</strong>
        </p>
      </div>

      <div className="report__tabs" role="tablist">
        {([["landscape", "競争環境"],
           ["tally", "多いもの・少ないもの"],
           ["map", `ポジショニングマップ${pmap ? ` (${list(pmap.placed).length})` : ""}`],
           ["sources", `出典 (${cited.length})`],
           ...(report.report_markdown
               ? ([["markdown", "文書"]] as const) : []),
          ] as const).map(([key, text]) => (
          <button key={key} role="tab" aria-selected={open === key}
                  className={open === key ? "is-active" : ""}
                  onClick={() => setOpen(key)}>
            {text}
          </button>
        ))}
        {report.report_markdown && (
          <button className="report__download" onClick={() => { void download(jobId); }}>
            Markdownで保存
          </button>
        )}
      </div>

      {open === "markdown" && report.report_markdown && (
        <MarkdownView markdown={report.report_markdown} jobId={jobId} />
      )}

      {open === "landscape" && (
        <div className="report__prose">
          <p>{json.landscape}</p>
          {list(json.crowded).length > 0 && (
            <>
              <h4>競争が集中している領域</h4>
              <ul>{list(json.crowded).map((x, i) => <li key={i}>{x}</li>)}</ul>
            </>
          )}
          {list(json.sparse).length > 0 && (
            <>
              <h4>比較的{label}が少ない領域</h4>
              <ul>{list(json.sparse).map((x, i) => <li key={i}>{x}</li>)}</ul>
            </>
          )}

          {opportunities.length > 0 && (
            <>
              <h4>機会仮説（仮説であって、結論ではありません）</h4>
              {opportunities.map((h, i) => (
                <div key={i} className="report__support">
                  <strong>{h.position}</strong>
                  <p>{h.why}</p>
                  {/* **但し書きを小さくしません。** 「競合が少ない」が
                      「機会がある」に読み替えられるのは、ここが薄いときです。 */}
                  <p className="report__counterpoint">
                    <strong>外れるとしたら</strong>　{h.caveat}
                  </p>
                </div>
              ))}
              <p className="report__note">
                {label}が少ないことは、そこに需要があることを意味しません。
                「まだ誰もやっていない」のか「やってみて成立しなかった」のかは、
                この分析では区別できません。
              </p>
            </>
          )}

          {list(json.not_determinable).length > 0 && (
            <>
              <h4>調べたが確認できなかったこと</h4>
              <ul>
                {list(json.not_determinable).map((x, i) => <li key={i}>{x}</li>)}
              </ul>
            </>
          )}
        </div>
      )}

      {open === "tally" && tally && <TallyView tally={tally} label={label} />}
      {open === "map" && pmap && <PositioningView map={pmap} label={label} />}
      {open === "sources" && <SourceList report={report} cited={cited} />}
      <p className="report__disclaimer">{report.disclaimer}</p>
    </div>
  );
}

/**
 * 何が多く、何が少ないか（指示書 §4）。**0 件の行を消しません。**
 *
 * 「この地域にインプラントを掲げる医院は無い」は、行が無いことでは
 * 伝わりません——調べ落としと区別が付かないからです。
 */
function TallyView({ tally, label }: { tally: CompetitorTally; label: string }) {
  return (
    <div className="report__prose">
      {([["取り扱っている領域", tally.products],
         ["訴求している顧客層", tally.targets],
         [`各${label}が掲げている強み（自由記述）`, tally.positioning],
        ] as [string, CountedLabel[]][]).map(([heading, rows]) => (
        list(rows).length > 0 && (
          <section key={heading}>
            <h4>{heading}</h4>
            <CountTable rows={list(rows)} label={label} />
          </section>
        )
      ))}
      {list(tally.place).length > 0 && (
        <section>
          <h4>立地・営業条件</h4>
          <CountTable rows={list(tally.place)} label={label} />
        </section>
      )}
      {tally.leaning_x_high_label && (
        <p>
          {tally.leaning_x_high_label}寄りと判定した{label}：
          <strong>{tally.leaning_x_high} 件</strong>
        </p>
      )}
      {tally.note && <p className="report__note">{tally.note}</p>}
    </div>
  );
}

function CountTable(
  { rows, label }: { rows: { label: string; count: number;
                            outside_vocabulary?: boolean }[]; label: string },
) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <table className="report__counts">
      <thead><tr><th>項目</th><th>{label}数</th><th /></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label} className={r.count === 0 ? "is-zero" : undefined}>
            <td>
              {r.label}
              {r.outside_vocabulary && (
                <small className="report__note">（設定の語彙外）</small>
              )}
            </td>
            <td>{r.count}</td>
            <td>
              <span className="report__bar"
                    style={{ width: `${(r.count / max) * 100}%` }} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * ポジショニングマップ（指示書 §5）。
 *
 * **判定困難を図に載せません。** 載せると、他の点と同じ確かさに見えます。
 * 置けなかった件は図の下に、理由とともに出します。
 */
function PositioningView(
  { map, label }: { map: PositioningMap; label: string },
) {
  const placed = list(map.placed);
  const scale = list(map.scale);
  const lo = Math.min(...(scale.length ? scale : [-2]));
  const hi = Math.max(...(scale.length ? scale : [2]));
  const span = hi - lo || 1;
  const at = (v: number) => ((v - lo) / span) * 100;

  return (
    <div className="report__prose">
      <div className="posmap" role="img"
           aria-label={`横軸 ${map.x.low}〜${map.x.high}、`
                       + `縦軸 ${map.y.low}〜${map.y.high} の分布図`}>
        <span className="posmap__axis posmap__axis--x-low">{map.x.low}</span>
        <span className="posmap__axis posmap__axis--x-high">{map.x.high}</span>
        <span className="posmap__axis posmap__axis--y-low">{map.y.low}</span>
        <span className="posmap__axis posmap__axis--y-high">{map.y.high}</span>
        {placed.map((p, i) => (
          <span key={i} className="posmap__point"
                style={{ left: `${at(p.x)}%`, bottom: `${at(p.y)}%` }}
                title={`${p.name}${p.basis ? `：${p.basis}` : ""}`}>
            <span className="posmap__label">{shorten(p.name, 10)}</span>
          </span>
        ))}
      </div>

      {list(map.quadrants).length > 0 && (
        <>
          <h4>区画ごとの{label}数</h4>
          <CountTable rows={list(map.quadrants)} label={label} />
          {/* **空いている区画を「機会」と読ませない。** 図だけでは、まだ誰も
              やっていないのか、やってみて成立しなかったのかを区別できません。 */}
          <p className="report__note">
            0 件の区画は、そこに機会があるという意味ではありません。
          </p>
        </>
      )}

      <h4>各{label}の位置</h4>
      <table className="report__counts">
        <thead>
          <tr><th>{label}</th><th>距離</th><th>判定の根拠</th></tr>
        </thead>
        <tbody>
          {[...placed].sort((a, b) => (a.distance_m ?? Infinity)
                                      - (b.distance_m ?? Infinity)).map((p, i) => (
            <tr key={i}>
              <td>{p.name}</td>
              <td>{p.distance_m == null ? "—"
                   : `${Math.round(p.distance_m).toLocaleString()}m`}</td>
              <td>{p.basis || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {list(map.undecided).length > 0 && (
        <>
          <h4>位置を判定できなかった{label}（{list(map.undecided).length} 件）</h4>
          <ul>
            {list(map.undecided).map((u, i) => (
              <li key={i}>{u.name}：{u.why}</li>
            ))}
          </ul>
        </>
      )}
      {map.note && <p className="report__note">{map.note}</p>}
    </div>
  );
}
