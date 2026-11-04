"""Six logical roles; legacy web_agent.agents class imports remain valid.

Each role depends on shared infrastructure, never another role's internals.
Pipeline owns the workflow and supplies the same optional model to A1/A2/A3.
"""
from .designer import Agent1TestDesigner
from .reviewer import Agent2TestReviewer
from .generator import Agent3SeleniumGenerator
from .executor import Agent4TestExecutor
from .verifier import Agent5EvidenceVerifier
from .recovery import Agent6RecoveryController

__all__ = [
    "Agent1TestDesigner", "Agent2TestReviewer", "Agent3SeleniumGenerator",
    "Agent4TestExecutor", "Agent5EvidenceVerifier", "Agent6RecoveryController",
]
