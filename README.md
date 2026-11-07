# WebAgent v0.3 — Selenium Web GUI Testing Agent

The v0.4 dynamic research demo now uses the existing six Agents with real DeepSeek
and local Chrome. With `DEEPSEEK_API_KEY`, `CHROME_BINARY` and `CHROMEDRIVER_PATH`
configured on the trusted control side, run:

```powershell
.\.venv\Scripts\python.exe run_demo.py --target dynamic --mode llm --provider deepseek --url 'https://demo.playwright.dev/todomvc/' --allow-live-site --allow-local-research --requirement 'Add Read book and Write tests. Complete Read book, check it is completed, select Active and verify only Write tests remains.'
```

The model writes actual Selenium Python in a capability-limited subset; the local
Worker does not receive API credentials. This is not an OS sandbox. See the local
Chinese report `docs/v04_phase2_demo_acceptance.md` for actual evidence and limits:
two successful runs, one technical failure, one interrupted run; three consecutive
successes and changed-input acceptance were not completed before the user stopped testing.

The v0.4 phase-1 infrastructure supports **local Windows Selenium + Chrome**;
it is **not wired into the six-agent pipeline yet**. Docker and LLM credentials
are unnecessary. Run the human-authored, reviewed Fixture programs from the repo root:

```powershell
.\.venv\Scripts\python.exe run_infrastructure_check.py --driver-path 'C:\path\to\chromedriver.exe'
```

Use an installed ChromeDriver matching Chrome (or set `CHROMEDRIVER_PATH`);
this entry does not automatically download drivers. Add `--show-browser` to watch.
It creates `artifacts/phase1_local_*/acceptance.json`, per-session logs, bounded
DOM observations and screenshots. Trial/Formal use separate Profiles and resettable
Fixture data; Windows Job Objects enforce deadlines and clean up owned processes.
**A local subprocess is not a security sandbox.** Arbitrary/model-generated Python
is disabled; only registered programs with a pinned reviewed-source hash can run.
File/network/CPU/memory permission isolation is not provided.
See `docs/v04_phase1_infrastructure_acceptance.md` for the local Chinese acceptance
report (the repository currently ignores `docs/`, so this file does not sync).
The old demos below still use their original trusted template path; they do not
accept arbitrary model Python. The new local acceptance covers infrastructure,
not Agent3 autonomous development or a real-model end-to-end run.

> **Six logical roles**, with a working evidence collection plan, targeted re-observation and bounded test-script recovery. Keeps DeepSeek / OpenRouter / OpenAI support from v0.2.2.
>
> **Scope**: arXiv advanced-search → results → abstract detail. This is a *restricted engineering prototype*, not a universal 6-LLM-agent browser automation platform.

## 1. Windows PowerShell: installation

Unzip **WebAgent03.zip**. Its root folder is `WebAgent`. Keep your working v0.2.2 separately.

```powershell
cd .\WebAgent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --retries 5
.\.venv\Scripts\python.exe -m pytest -q
```

No PowerShell activation is necessary; always run the project's `.venv` Python to avoid accidentally invoking Anaconda's packages. Python 3.11/3.12 and Chrome are recommended. If the mirror is unavailable, remove the `-i ...` option.

## 2. Three demonstrations (no API key needed)

**Demo A — Normal full workflow (expected PASS)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode template --show-browser --demo-delay 0.7
```

**Demo B — Agent5 active evidence acquisition (expected PASS after one probe)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode template --demo-evidence-gap --show-browser --demo-delay 0.7
```

Local-only *controlled fault injection*: the recorded `detail.title` is deliberately omitted after the first browser execution; Agent5 identifies the missing oracle evidence and requests Agent4 to **open that same paper detail in a fresh Selenium browser session** and read only the missing field. Inspect `controlled_evidence_gap.json`, `probe_request.json`, `probe_result.json`, `collection_log` and `report.html`. This is an **evidence collection simulation**, not a discovered arXiv bug.

**Demo C — Agent6 bounded locator recovery (expected PASS after first ERROR and one repair)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode template --demo-broken-locator --show-browser --demo-delay 0.7
```

Another *controlled fault injection*: the initial Selenium script is generated with a broken keyword locator. Agent6 classifies the `locate_keyword` error, requests **one allowlisted locator-only regeneration** from Agent3, and Agent4 executes the new script. Inspect `repair_request.json`, `generated_test_attempt2.py`, `evidence_attempt2.json`, `recovery_history.json` and the report. The original test contract/oracles do not change.

**Demo D — Known faulty application (expected FAIL, no automatic repair)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture-bug --mode template --show-browser
```

The injected bug deliberately changes a search-list title but not its matching detail title. **FAIL is expected**; the CLI exit code is `1` because the test failed. The system must not rewrite a correct oracle to turn this into PASS.

You may disable active mechanisms in the CLI using `--max-probes 0` and/or `--max-repairs 0`. v0.3 permits at most one targeted probe and one code repair per run. The fault-injection flags are accepted *only for local fixtures*.

## 3. Streamlit demo

```powershell
.\.venv\Scripts\python.exe -m streamlit run app_ui.py
```

The UI exposes the target, template/LLM mode, local fault injection, probe/repair budgets, actual screenshots, generation audit, acquisition logs and recovery decisions. The screenshot tab is for viewing evidence, **not** a real-time desktop mirror. Use `--show-browser` or uncheck the headless option to show Selenium driving Chrome.

## 4. Real DeepSeek / OpenRouter LLM

Use the **same `.env.example` and LLM adapter as v0.2.2**. DeepSeek and OpenRouter use OpenAI-compatible API endpoints, with their *own* provider credentials (not an OpenAI API key). Create a `.env` and never commit it or include it in screen recordings.

```powershell
Copy-Item .env.example .env
notepad .env
```

For DeepSeek, fill in `LLM_PROVIDER=deepseek`, `DEEPSEEK_API_KEY=...`, `DEEPSEEK_MODEL=...`. Use a model ID supported by your account/provider. For OpenRouter, fill in `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY=...`, `OPENROUTER_MODEL=provider/model` (replace it with an available model ID).

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode llm --provider deepseek --show-browser
# or: --provider openrouter
```

LLM mode uses the actual model for Agent1's bounded query intent, Agent2's independent review and Agent3's locator ranking. Agent4 remains deterministic Selenium execution; Agent5 and Agent6 have **real decision logic**, but are *not* independent LLM instances. This design avoids running arbitrary generated Python from untrusted model output.

## 5. What changed in v0.3?

| Role | v0.3 execution | Main output |
|---|---|---|
| Agent1 | Parse bounded natural language search intent | `draft_cases.json` |
| Agent2 | Review intent, freeze oracles, issue `Evidence Plan` (4 checkpoints for a full search/detail run) | `test_contract.json` |
| Agent3 | Read current DOM using DOM Inspector, select allowlisted locators, render reviewable Selenium Python; regenerate limited locators on approved repair | `generated_test.py`, `generated_test_attempt2.py` if needed |
| **Agent4** | Selenium GUI operations; execute evidence checkpoints, selectively take requested screenshots, preserve acquisition logs; execute independent detail re-observation | `evidence.json`, `collection_log`, `probe_result.json` |
| **Agent5** | Check oracles; identify *missing* eligible evidence; issue a bounded `detail_reobserve` request, then re-evaluate | `probe_request.json`, revised verdict |
| **Agent6** | Diagnose bounded locator errors (keyword/field/CS control); request one regenerating pass; route terminal results and keep history | `repair_request.json`, `recovery_history.json`, `final_verdict.json`, `report.html` |

Agent4's collectors are **plan-driven for the supported search workflow**, not an arbitrary-evidence DSL; only allowlisted fields/checkpoints are available. Agent5's interactive verification is currently restricted to missing *paper detail title/ID* evidence. Agent6 can repair a subset of locator failures, not timeouts due to network, CAPTCHA or application failures.

### v0.3 execution data flow

```
Requirements → Agent1 → Agent2: Test Contract + Evidence Plan
                                    │
                                    ▼
                       Agent3: DOM Inspector + script
                                    │
                                    ▼
                         Agent4: Selenium + evidence
                                    │
                                    ▼
                         Agent5: oracle verification
                           │                 │
                 missing detail data    execution ERROR
                           │                 │
                   targeted probe     Agent6 repair decision
                           │                 │
                   Agent4 reobserve   Agent3 regenerate → Agent4 rerun
                           └────────┬────────┘
                                    ▼
                           final HTML report
```

All original and repaired script/source files are retained. Each run has its own timestamped `artifacts/<run_id>/` folder, and `report.html` includes checkpoint observations, probes and repair history.

## 6. Targeting real arXiv

The baseline arXiv Selenium run has been demonstrated by the user on **v0.2.2**. **v0.3 has not been executed against arxiv.org in this build environment.** The site may change, require consent or block automated requests. `--target arxiv` therefore requires `--allow-live-site` and the operator's confirmation of acceptable usage.

Example *only when you have permission*:

```powershell
.\.venv\Scripts\python.exe run_demo.py --target arxiv --allow-live-site --mode llm --provider deepseek --show-browser
```

By default, the real target **does not** automatically trigger additional probe or repair visits, even though local fixtures can. To explicitly permit one extra visit / test rerun under appropriate authorization, use `--enable-live-probes` and/or `--enable-live-recovery`. Do not circumvent access controls, login requirements or rate limits. This tool is for low-frequency, permitted testing, not scraping.

## 7. Supported / unsupported

- Supported: single arXiv-like advanced search; keyword, field, CS checkbox; optional open-first-result and cross-page ID/title checks; screenshot & DOM evidence; basic issue classification; bounded recovery for known locator errors.
- Unsupported: arbitrary websites, login/payment, large-scale crawling, universal GUI repair, unconstrained LLM-produced code, validating publication facts or judging arbitrary UI visual defects.
- `--only-generate` only creates a script; the verdict must be `INCONCLUSIVE`, never a fake PASS.

## 8. Verification status

- **45/45 offline tests passed** at packaging time: 34 baseline regressions plus 11 new evidence-plan/probe/recovery tests, including mocked subprocess integration.
- **Actual Selenium+Chrome v0.3 end-to-end tests were not run here** because this sandbox lacks the Selenium package and external package network access. Please run the four commands in §2 on your Windows system before calling v0.3 fully validated.
- Actual paid provider API calls were not made without credentials. Do not paste your API keys into chat.

Read `VERIFICATION_STATUS.md` for the detailed scope and test limits.
