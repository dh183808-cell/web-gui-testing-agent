"""Opt-in real local Chrome acceptance. Only reviewed trusted scripts execute."""
import os
from pathlib import Path
import uuid

import pytest

from run_infrastructure_check import run_acceptance


@pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS') != '1',
                    reason='Real Chrome acceptance requires WEBAGENT_RUN_BROWSER_TESTS=1')
def test_real_local_sessions_replay_and_cleanup():
    root = Path(__file__).resolve().parents[1] / 'artifacts' / ('pytest_phase1_' + uuid.uuid4().hex[:8])
    result = run_acceptance(root)
    assert result['status'] == 'PASSED', f"Real acceptance not passed: {root / 'acceptance.json'}"
