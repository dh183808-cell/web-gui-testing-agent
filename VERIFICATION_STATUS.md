# WebAgent v0.3 | Verification Status

## Offline validation

- 45/45 `pytest` tests passed (34 retained regression tests + 11 added v0.3 tests).
- Static Python compilation (`compileall`) passed.
- New coverage includes: Evidence Plan checkpoint dispatch, selective screenshot capture, missing evidence tracking, limited field allowlists, target-origin validation for probes, non-overwriting evidence merge, terminal app FAIL without re-generation, bounded Agent6 locator recovery, generated two-version script parsing, end-to-end pipeline with mocked subprocesses for probe and repair, and prevention of fault injection against real arxiv.org.
- CLI `--only-generate` smoke test checked independently (requires no browser).

## Not verified in this environment

- **Actual Selenium v0.3 browser runs**: the sandbox could not install Selenium due to lack of DNS/network access to the Python package index. The user's previous arxiv.org PASS was on *v0.2.2*, not this build.
- Real DeepSeek / OpenRouter API calls: require provider credentials.
- Live arxiv.org testing: not performed, and may require authorization under site terms.

## Exact scope

- Agent4 actually reads the scoped Evidence Plan to decide checkpoint observations and screenshot timing for an arXiv search/detail workflow.
- Agent5 uses a bounded deterministic evidence-gap planner and opens a second Selenium session only when required detail title/ID observations are missing and authorized.
- Agent6 makes bounded rule-based recovery decisions for keyword, search-field and CS locator failures. Agent3 regenerates a safe Python template from the existing DOM candidate pool; one retry budget; no arbitrary model code execution.
- Controlled fault injection is clearly labeled and applies only to the local fixture.
- Neither active verification nor recovery makes this a general-purpose autonomous multi-agent application tester.
