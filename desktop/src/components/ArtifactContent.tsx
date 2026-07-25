import { CheckCircle, WarningCircle } from "@phosphor-icons/react";
import Markdown from "react-markdown";
import { asRecord, readString, readStringList } from "../lib/values";

type UnifiedDiffLine = {
  kind: "add" | "remove" | "context" | "meta" | "hunk";
  content: string;
  oldLine: number | null;
  newLine: number | null;
};

type DiffFileSummary = {
  path: string;
  additions: number;
  deletions: number;
  binary: boolean;
};

function readArtifactJson(content: string): Record<string, unknown> | null {
  try {
    return asRecord(JSON.parse(content));
  } catch {
    return null;
  }
}

function parseUnifiedDiff(content: string): UnifiedDiffLine[] {
  let oldLine: number | null = null;
  let newLine: number | null = null;
  return content.replace(/\r\n/g, "\n").split("\n").map((line) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      return { kind: "hunk", content: line, oldLine: null, newLine: null };
    }
    if (
      line.startsWith("+++") ||
      line.startsWith("---") ||
      line.startsWith("diff ") ||
      line.startsWith("index ") ||
      line.startsWith("\\ No newline")
    ) {
      return { kind: "meta", content: line, oldLine: null, newLine: null };
    }
    if (line.startsWith("+")) {
      const result = { kind: "add" as const, content: line, oldLine: null, newLine };
      newLine = newLine === null ? null : newLine + 1;
      return result;
    }
    if (line.startsWith("-")) {
      const result = { kind: "remove" as const, content: line, oldLine, newLine: null };
      oldLine = oldLine === null ? null : oldLine + 1;
      return result;
    }
    if (line.startsWith(" ")) {
      const result = { kind: "context" as const, content: line, oldLine, newLine };
      oldLine = oldLine === null ? null : oldLine + 1;
      newLine = newLine === null ? null : newLine + 1;
      return result;
    }
    return { kind: "meta", content: line, oldLine: null, newLine: null };
  });
}

function summarizeUnifiedDiff(content: string): DiffFileSummary[] {
  const files: DiffFileSummary[] = [];
  let current: DiffFileSummary | null = null;
  for (const line of content.replace(/\r\n/g, "\n").split("\n")) {
    const header = /^diff --git a\/(.+) b\/(.+)$/.exec(line);
    if (header) {
      current = { path: header[2], additions: 0, deletions: 0, binary: false };
      files.push(current);
      continue;
    }
    if (!current) continue;
    if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) {
      current.binary = true;
    } else if (line.startsWith("+") && !line.startsWith("+++")) {
      current.additions += 1;
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      current.deletions += 1;
    }
  }
  return files;
}

function DiffSummary({ files }: { files: DiffFileSummary[] }) {
  const additions = files.reduce((total, file) => total + file.additions, 0);
  const deletions = files.reduce((total, file) => total + file.deletions, 0);
  const visibleFiles = files.slice(0, 12);
  return (
    <section className="diff-summary" aria-label="代码变更摘要">
      <div className="diff-summary-facts">
        <span><b>{files.length}</b> 个文件</span>
        <span className="diff-add"><b>+{additions}</b> 新增</span>
        <span className="diff-remove"><b>-{deletions}</b> 删除</span>
      </div>
      {files.length > 0 ? (
        <ul className="diff-file-list">
          {visibleFiles.map((file) => (
            <li key={file.path}>
              <code title={file.path}>{file.path}</code>
              {file.binary ? (
                <span className="diff-binary">二进制</span>
              ) : (
                <span><i className="diff-add">+{file.additions}</i><i className="diff-remove">-{file.deletions}</i></span>
              )}
            </li>
          ))}
          {files.length > visibleFiles.length && (
            <li className="diff-file-more">其余 {files.length - visibleFiles.length} 个文件请查看下方完整 Diff</li>
          )}
        </ul>
      ) : (
        <p>当前产物没有可解析的文件级变更。</p>
      )}
    </section>
  );
}

/** 根据服务端产物类型提供确定性展示，不推断或改写任务结论。 */
export function ArtifactContent({ kind, content }: { kind: string; content: string }) {
  if (kind === "git_diff") {
    const lines = parseUnifiedDiff(content);
    return (
      <div className="artifact-content diff-view" aria-label="代码变更 Diff">
        <DiffSummary files={summarizeUnifiedDiff(content)} />
        <div className="diff-code-lines">
          {lines.map((line, index) => (
            <div className={`diff-line diff-${line.kind}`} key={index}>
              <span>{line.oldLine ?? ""}</span><span>{line.newLine ?? ""}</span>
              <code>{line.content || " "}</code>
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (kind === "report" || kind === "plan_markdown") {
    return <article className="artifact-content markdown-content"><Markdown>{content}</Markdown></article>;
  }
  const data = readArtifactJson(content);
  if (kind === "verification" && data) {
    const status = readString(data.status) ?? "UNKNOWN";
    const success = status === "PASSED";
    const reports = readStringList(data.surefire_reports);
    const argv = readStringList(data.argv);
    const stdout = readString(data.stdout_summary);
    const stderr = readString(data.stderr_summary);
    return (
      <div className="artifact-content verification-view">
        <div className={`verification-verdict ${success ? "passed" : "failed"}`}>
          {success ? <CheckCircle size={20} weight="fill" /> : <WarningCircle size={20} weight="fill" />}
          <div><strong>{success ? "验证通过" : "验证未通过"}</strong><span>{status}</span></div>
        </div>
        <dl className="artifact-facts">
          <div><dt>Recipe</dt><dd>{readString(data.recipe) ?? "未记录"}</dd></div>
          <div><dt>退出码</dt><dd>{String(data.exit_code ?? "未记录")}</dd></div>
          <div><dt>耗时</dt><dd>{typeof data.duration_ms === "number" ? `${data.duration_ms.toLocaleString()} ms` : "未记录"}</dd></div>
          <div><dt>审计代码</dt><dd>{readString(data.code) ?? "未记录"}</dd></div>
        </dl>
        {argv.length > 0 && <code className="artifact-command">{argv.join(" ")}</code>}
        {reports.length > 0 && <section className="artifact-list-section"><h3>Surefire 报告</h3><ul>{reports.map((report) => <li key={report}>{report}</li>)}</ul></section>}
        {(stdout || stderr) && <details className="artifact-details"><summary>查看截断后的 Maven 输出摘要</summary>{stdout && <pre>{stdout}</pre>}{stderr && <pre>{stderr}</pre>}</details>}
      </div>
    );
  }
  if (kind === "plan_json" && data) {
    const candidates = readStringList(data.candidate_files);
    const steps = readStringList(data.steps);
    const evidence = Array.isArray(data.evidence)
      ? data.evidence.map(asRecord).filter(Boolean) as Record<string, unknown>[]
      : [];
    return (
      <div className="artifact-content plan-view">
        <section className="plan-summary"><span>问题摘要</span><p>{readString(data.summary) ?? readString(data.problem_summary) ?? "计划未提供问题摘要。"}</p></section>
        <section className="artifact-list-section"><h3>候选文件</h3><ul className="path-list">{candidates.map((path) => <li key={path}>{path}</li>)}</ul>{candidates.length === 0 && <p>尚未确认可修改文件。</p>}</section>
        <section className="artifact-list-section"><h3>修改步骤</h3><ol>{steps.map((step, index) => <li key={index}>{step}</li>)}</ol>{steps.length === 0 && <p>本任务未生成写入步骤。</p>}</section>
        <section className="artifact-list-section"><h3>验证建议</h3><p>{readStringList(data.verification).join("；") || "未记录额外验证建议。"}</p><code className="artifact-command">{readString(data.verification_recipe) ?? "未指定 Recipe"}</code></section>
        {evidence.length > 0 && <section className="artifact-list-section"><h3>来源证据</h3><ul className="path-list">{evidence.map((item, index) => {
          const path = readString(item.path) ?? "未知来源";
          const lineStart = typeof item.line_start === "number" ? `:${item.line_start}` : "";
          const note = readString(item.note);
          return <li key={path + index}><code>{path + lineStart}</code>{note && <span>{note}</span>}</li>;
        })}</ul></section>}
      </div>
    );
  }
  if (kind === "patch_proposal" && data) {
    const changes = Array.isArray(data.changes)
      ? data.changes.map(asRecord).filter(Boolean) as Record<string, unknown>[]
      : [];
    return (
      <div className="artifact-content patch-view">
        <section className="plan-summary"><span>补丁摘要</span><p>{readString(data.summary) ?? "补丁提案未提供摘要。"}</p></section>
        <dl className="artifact-facts"><div><dt>Recipe</dt><dd>{readString(data.recipe) ?? "未记录"}</dd></div><div><dt>目标测试</dt><dd>{readString(data.test_class) ?? "未指定"}</dd></div></dl>
        <section className="artifact-list-section"><h3>待修改文件</h3><ul className="path-list">{changes.map((change, index) => <li key={readString(change.path) ?? String(index)}>{readString(change.path) ?? "未命名文件"}</li>)}</ul></section>
      </div>
    );
  }
  if (kind === "telemetry" && data) {
    const model = asRecord(data.model);
    const budget = asRecord(data.budget);
    return (
      <div className="artifact-content telemetry-view">
        <dl className="artifact-facts">
          <div><dt>节点</dt><dd>{String(data.node_count ?? "未记录")}</dd></div>
          <div><dt>总耗时</dt><dd>{typeof data.node_total_duration_ms === "number" ? `${data.node_total_duration_ms.toLocaleString()} ms` : "未记录"}</dd></div>
          <div><dt>Token</dt><dd>{typeof model?.total_tokens === "number" ? model.total_tokens.toLocaleString() : "未记录"}</dd></div>
          <div><dt>预算</dt><dd>{readString(budget?.status) ?? "未记录"}</dd></div>
        </dl>
        <pre className="artifact-raw-content">{content}</pre>
      </div>
    );
  }
  return <pre className="artifact-content artifact-raw-content">{content}</pre>;
}
