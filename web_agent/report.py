"""Self-contained, evidence-linked final HTML report."""
from __future__ import annotations

from datetime import datetime
from html import escape
import json
from pathlib import Path


def html_report(run_dir: Path, contract: dict, evidence: dict, verdict: dict,
                *, inspection: dict | None = None) -> Path:
    def esc(x): return escape(str(x), quote=True)
    status = verdict["status"]
    palette = {"PASS": ("#177447", "#eaf8f0"), "FAIL": ("#a72b36", "#fff0f0"),
               "ERROR": ("#a4630c", "#fff6e6"),
               "INCONCLUSIVE": ("#58657e", "#f0f3f9")}
    color, bg = palette.get(status, ("#58657e", "#f0f3f9"))
    def checks_html():
        if not verdict.get("checks"):
            return '<tr><td colspan="4" class="muted">没有可判定的完整检查项。</td></tr>'
        return "".join(
            f'<tr><td>{esc(c["oracle_id"])} · {esc(c["name"])}</td>'
            f'<td>{"?" if c["passed"] is None else "✓" if c["passed"] else "✗"}</td><td>{esc(c["expected"])}</td>'
            f'<td>{esc(c["actual"])}</td></tr>'
            for c in verdict["checks"])
    screenshots = "".join(
        f'<a href="{esc(name)}"><img src="{esc(name)}" alt="{esc(name)}">'
        f'<span>{esc(name)}</span></a>' for name in evidence.get("screenshots", [])
        if (run_dir/name).is_file())
    events = "".join(
        f'<tr><td>{i}</td><td>{esc(s.get("stage", ""))}</td>'
        f'<td><code>{esc(json.dumps(s, ensure_ascii=False)[:400])}</code></td></tr>'
        for i, s in enumerate(evidence.get("steps", []), start=1))
    target_label = "本地合成测试站（非 arXiv）" if contract["target"].startswith("fixture") else "arXiv 真实站点（未获得缺陷确认）"
    diagnostics = evidence.get("diagnostics") or {}
    error_phase = evidence.get("error_stage")
    diagnostics_html = ""
    if status == "ERROR":
        details = json.dumps(diagnostics, ensure_ascii=False, indent=2)
        diagnostics_html = (
            '<section><h2>执行错误诊断（Agent4）</h2>'
            f'<p><b>失败阶段：</b><code>{esc(error_phase or "unknown")}</code></p>'
            f'<p><b>错误描述：</b>{esc(evidence.get("error") or "")}</p>'
            f'<pre>{esc(details)}</pre>'
            '<p class="muted">详细原始堆栈见 evidence.json 的 traceback_tail；'
            '<a href="failure_diagnostics.json">独立诊断文件</a>（仅在采集成功时存在）。</p></section>'
        )
    acquisition = evidence.get("collection_log", [])
    probes = evidence.get("probes", [])
    attempts = evidence.get("attempt_history", [])
    repair_history = verdict.get("recovery_history", [])
    audit_sections = (
        '<section><h2>Agent4 · 计划驱动取证</h2>'
        f'<pre>{esc(json.dumps(acquisition, ensure_ascii=False, indent=2)[:12000])}</pre></section>'
        '<section><h2>Agent5 · 主动补证</h2>'
        f'<pre>{esc(json.dumps(probes, ensure_ascii=False, indent=2)[:8000])}</pre></section>'
        '<section><h2>Agent6 · 代码修复与重跑</h2>'
        f'<pre>{esc(json.dumps({"attempts": attempts, "repairs": repair_history}, ensure_ascii=False, indent=2)[:10000])}</pre></section>'
    )
    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>Web GUI Agent · {esc(status)} · {esc(contract['case_id'])}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{background:#f7f9fc;color:#1b2740;font:15px/1.65 system-ui,-apple-system,"Microsoft Yahei",sans-serif;margin:0}}
main{{max-width:1120px;margin:35px auto;padding:0 28px}}header{{background:#132647;color:white;padding:30px max(25px,calc((100vw - 1120px)/2))}}
header small{{color:#bed1e9}}h1{{font-size:28px;margin:2px 0}}h2{{font-size:19px;margin-top:3px}}section{{background:white;border:1px solid #e0e8f1;border-radius:13px;padding:23px;margin:22px 0;box-shadow:0 5px 24px #182c4d08}}
.meta{{display:flex;gap:22px;flex-wrap:wrap;color:#61708b}}.pill{{display:inline-block;padding:7px 12px;border-radius:7px;font-weight:800;background:{bg};color:{color}}}
table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #e9edf4;padding:10px 12px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{color:#63748e;font-size:12px;text-transform:uppercase}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}}.gallery a{{border:1px solid #dfe7f2;padding:7px;border-radius:10px;color:#234f96;text-decoration:none}}
.gallery img{{width:100%;height:170px;object-fit:contain;background:#f3f6fa;border-radius:5px}}.gallery span{{display:block;font-size:12px;padding:6px 3px}}
pre{{background:#f0f3f9;padding:15px;white-space:pre-wrap;overflow-wrap:anywhere;border-radius:8px}}code{{font-size:12px}}.muted{{color:#64748b}}a{{color:#2164ad}}
</style></head><body>
<header><small>Six-Agent Web GUI Testing · Demo v0.3</small><h1>自动化测试与证据复核报告</h1><div class="meta"><span>Case: {esc(contract['case_id'])}</span><span>{esc(target_label)}</span><span>{esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}</span></div></header>
<main><section><div class="pill">{esc(status)}</div><h2>{esc(verdict['message'])}</h2>
<p class="muted">来源模式：{esc(contract['mode'])} · 修复执行：{str(verdict.get('auto_repair_performed', False))} · 恢复策略：{esc(verdict.get('recovery_policy',''))}</p>
<p><b>自然语言需求：</b>{esc(contract['requirement'])}</p></section>
<section><h2>Oracle 检查结果</h2><table><thead><tr><th>检查点</th><th>结果</th><th>预期</th><th>实际</th></tr></thead>
<tbody>{checks_html()}</tbody></table></section>
<section><h2>关键截图</h2><div class="gallery">{screenshots or '<p class="muted">无截图。浏览器可能未成功启动。</p>'}</div></section>
{diagnostics_html}
{audit_sections}
<section><h2>执行轨迹</h2><table><thead><tr><th>#</th><th>阶段</th><th>证据摘要</th></tr></thead><tbody>{events}</tbody></table></section>
<section><h2>执行约束与数据来源</h2><p>只访问指定检索网页和论文详情。不进行登录、PDF 下载、批量抓取或绕过访问限制。</p>
<p>在真实网站上，跨页面不一致只代表可疑现象，不自动构成已确认软件缺陷；需要人工复核。</p>
<p>由 Agent2 锁定测试预言；Agent3 生成可检查的 Selenium 脚本；Agent4 采集运行证据；Agent5 比较；Agent6 形成最终报告。</p>
<p class="muted">原始结构化产物：<a href="test_contract.json">test_contract.json</a> · <a href="evidence.json">evidence.json</a> · <a href="final_verdict.json">final_verdict.json</a> · <a href="generated_test.py">generated_test.py</a> · <a href="review_decision.json">review_decision.json</a> · <a href="generation_audit.json">generation_audit.json</a></p>
</section></main></body></html>'''
    path = run_dir / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path
