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


def dynamic_html_report(run_dir: Path, result: dict) -> Path:
    """Render the stage-4 dynamic result without assuming v0.3 fields."""
    run_dir = Path(run_dir)
    verdict = result.get('verdict', {})
    contract = result.get('contract')
    if contract is None:
        contract = {'protocol_version': '0.4', 'document_type': 'test_design',
                    'design': result.get('design', {}), 'design_digest': None}
    design = contract.get('design', contract)
    checks = verdict.get('checks', [])
    rows = ''.join(f"<tr><td>{escape(str(c.get('oracle_id')))}</td><td>{escape(str(c.get('passed')))}</td>"
                   f"<td>{escape(str(c.get('expected')))}</td><td>{escape(str(c.get('actual')))}</td></tr>" for c in checks)
    steps = result.get('steps', [])
    step_rows = ''.join(f"<tr><td>{escape(str(s.get('step_id')))}</td><td>{escape(str(s.get('status')))}</td>"
                        f"<td>{escape(str(s.get('attempt', 1)))}</td><td>{escape(str(s.get('observation_id')))}</td>"
                        f"<td><code>{escape(str(s.get('source_sha256','')))}</code></td></tr>" for s in steps)
    html = f'''<!doctype html><meta charset="utf-8"><title>WebAgent v0.4 dynamic report</title>
<style>body{{font:15px system-ui;margin:30px auto;max-width:1200px;color:#17243d}}section{{border:1px solid #dce5f0;border-radius:10px;padding:18px;margin:18px 0}}table{{width:100%;border-collapse:collapse}}td,th{{padding:8px;border-bottom:1px solid #e5eaf2;text-align:left}}pre{{white-space:pre-wrap;background:#f3f6fa;padding:12px}}.status{{font-size:22px;font-weight:bold}}</style>
<h1>WebAgent v0.4 动态测试报告</h1><p class="status">{escape(str(verdict.get('status')))} · {escape(str(verdict.get('message')))}</p>
<section><h2>原始需求</h2><p>{escape(str(design.get('requirement')))}</p><p>目标：{escape(str(design.get('target_url')))}</p>
<h3>协议</h3><pre>{escape(json.dumps({'protocol_version': contract.get('protocol_version'), 'design_digest': result.get('contract_digest'), 'target_config': result.get('target_config')},ensure_ascii=False,indent=2))}</pre></section>
<section><h2>Agent2 冻结契约</h2><pre>{escape(json.dumps(design,ensure_ascii=False,indent=2))}</pre></section>
<section><h2>步骤执行与 Agent3 源码摘要</h2><table><tr><th>步骤</th><th>状态</th><th>尝试</th><th>观察</th><th>源码 SHA-256</th></tr>{step_rows}</table></section>
<section><h2>Agent5 Oracle 结果</h2><table><tr><th>Oracle</th><th>结果</th><th>预期</th><th>实际</th></tr>{rows or '<tr><td colspan="4">无可判定 Oracle</td></tr>'}</table><pre>{escape(json.dumps(result.get('evidence',{}),ensure_ascii=False,indent=2)[:30000])}</pre></section>
<section><h2>Agent5 补证与 Agent6 恢复</h2><pre>{escape(json.dumps({'probes':result.get('probes',[]),'recovery_history':result.get('recovery_history',[])},ensure_ascii=False,indent=2))}</pre></section>
<section><h2>执行阶段</h2><pre>{escape(json.dumps(result.get('stages',[]),ensure_ascii=False,indent=2))}</pre></section>'''
    def json_section(title, value):
        return f'<section><h2>{escape(title)}</h2><pre>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre></section>'
    html += json_section('Agent2 审核记录', result.get('reviews', []))
    html += json_section('步骤级状态', result.get('step_states', {}))
    html += json_section('Oracle 与证据关联', checks)
    for title, pattern in [('Agent3 实际生成源码', 'generation/*/dynamic_*.py'),
                           ('Agent3 定位审计', 'generation/*/generation_*.json'),
                           ('实时 DOM Observation', 'observation_*.json')]:
        html += '<section><h2>' + escape(title) + '</h2>'
        for artifact in sorted(run_dir.glob(pattern)):
            if not artifact.resolve().is_relative_to(run_dir.resolve()) or not artifact.is_file():
                continue
            relative = artifact.relative_to(run_dir).as_posix()
            from urllib.parse import quote
            html += f'<details><summary><a href="{escape(quote(relative),quote=True)}">{escape(relative)}</a></summary><pre>{escape(artifact.read_text(encoding="utf-8")[:30000])}</pre></details>'
        html += '</section>'
    html += json_section('实际执行轨迹', result.get('steps', []))
    html += '<p>PASS 仅表示当前冻结契约的必要 Oracle 通过。规划完成和执行步骤完成本身不代表 PASS。线上模型/网站能力需单独验证。</p>'
    path = run_dir / 'dynamic_report.html'
    path.write_text(html, encoding='utf-8')
    return path
