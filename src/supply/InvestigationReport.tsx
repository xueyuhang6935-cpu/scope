type EvidenceStatement = { statement: string; evidenceIds: string[]; qualification?: string };
type Claim = EvidenceStatement & { kind: 'observed' | 'reported' | 'inferred'; limitation: string };
type Evidence = { id: string; record: Record<string, unknown>; provenance?: { sourceType?: string; truncated?: boolean; containsSupplierStatements?: boolean } };
export type Report = {
  mode: string; status?: 'complete' | 'partial'; facts: EvidenceStatement[]; hypotheses: EvidenceStatement[];
  missing: string[]; trace: Array<{ tool: string; status?: string; items?: number; truncated?: boolean; error?: { message: string } }>;
  modelCalls: number; tokens: number | null; stopReason: string; evidence?: Evidence[];
  modelSummary?: { summary: string; evidenceIds: string[]; qualification: string; status?: 'complete' | 'partial'; claims?: Claim[]; unresolvedQuestions?: string[]; nextSteps?: string[] } | null;
  planRevisions?: Array<{ revision: number; reason: string; steps: string[] }>; planBlockers?: string[];
};

function EvidenceDetails({ evidence, id }: { evidence?: Evidence; id: string }) {
  return <details><summary style={{ overflowWrap: 'anywhere' }}>证据：{id}</summary>
    {!evidence ? <p>此历史报告未保存可展开的证据内容，请核对原始记录。</p> : <>
      {(evidence.provenance?.truncated || evidence.record.truncated === true) && <p>仅展示部分结果，不能据此推断全部数据。</p>}
      {(evidence.provenance?.sourceType === 'supplier_statement' || evidence.provenance?.containsSupplierStatements) && <p>含供应商陈述，相关原因尚需独立核实。</p>}
      <pre style={{ overflowX: 'auto', maxWidth: '100%', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(evidence.record, null, 2)}</pre>
    </>}
  </details>;
}

export default function InvestigationReport({ report }: { report: Report }) {
  const evidence = new Map(report.evidence?.map(e => [e.id, e]));
  const model = report.modelSummary;
  const structured = Array.isArray(model?.claims);
  const references = (ids: string[]) => ids.map(id => <EvidenceDetails key={id} id={id} evidence={evidence.get(id)} />);
  const groups = [['observed', '数据直接显示的事实'], ['reported', '外部主体陈述'], ['inferred', '推断与局限']] as const;
  return <section className="sp-investigation-report" aria-label="调查报告">
    <h3>调查结论与证据</h3>
    {(report.status === 'partial' || model?.status === 'partial') && <p role="status"><strong>调查未完成</strong> · 已保留有效发现，未解决的问题列在下方。</p>}
    <p>{report.mode === 'model' ? '模型工具调查' : '确定性工具调查'} · 模型调用 {report.modelCalls} 次 · Token {report.tokens ?? '未返回用量'} · {report.stopReason}</p>
    {report.planRevisions?.map(p => <details key={p.revision}><summary>调查计划第 {p.revision} 版 · {p.reason}</summary><ol>{p.steps.map((step, i) => <li key={i}>{step}</li>)}</ol></details>)}
    {!!report.facts.length && <h4>确定性计算结果</h4>}
    {report.facts.map((f, i) => <details key={i}><summary>{f.statement}</summary>{references(f.evidenceIds)}</details>)}
    {structured ? <>
      {groups.map(([kind, label]) => {
        const claims = model.claims!.filter(c => c.kind === kind);
        return claims.length > 0 && <div key={kind}><h4>{label}</h4>{claims.map((claim, i) => <article key={i}>
          <p>{claim.statement}</p>{claim.limitation && <p>局限：{claim.limitation}</p>}{references(claim.evidenceIds)}
        </article>)}</div>;
      })}
      {!model.claims!.length && <p>{model.summary}</p>}
      {!!model.unresolvedQuestions?.length && <><h4>未解决问题</h4><ul>{model.unresolvedQuestions.map((q, i) => <li key={i}>{q}</li>)}</ul></>}
      {!!model.nextSteps?.length && <><h4>建议补充的证据与下一步</h4><ul>{model.nextSteps.map((s, i) => <li key={i}>{s}</li>)}</ul><p>以上为建议，尚未执行。</p></>}
      <small>{model.qualification}</small>
    </> : <>
      {!!report.hypotheses.length && <h4>待核实原因</h4>}
      {report.hypotheses.map((f, i) => <div key={i}><p>{f.statement}（{f.qualification}）</p>{references(f.evidenceIds)}</div>)}
      {model && <div><p>{model.summary}</p><small>{model.qualification}</small>{references(model.evidenceIds)}</div>}
    </>}
    {report.missing.map(m => <p role="alert" key={m}>{m}</p>)}
    {!!report.planBlockers?.length && <><h4>方案处理受阻原因</h4><ul>{report.planBlockers.map((reason, i) => <li key={i}>{reason}</li>)}</ul></>}
    <details><summary>工具调用轨迹</summary>{report.trace.map((t, i) => <p key={i}>{t.tool} · {t.status === 'error' ? '查询失败' : '已完成'}{t.items !== undefined && ` · 返回 ${t.items} 项`}{t.truncated && ' · 仅展示部分结果，需缩小范围继续查询'}{t.error && <small>{t.error.message}</small>}</p>)}</details>
    <details><summary>核对调查原始证据</summary>{report.evidence?.map(e => <EvidenceDetails key={e.id} id={e.id} evidence={e} />)}</details>
  </section>;
}
