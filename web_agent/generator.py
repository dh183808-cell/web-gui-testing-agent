"""Backward-compatible Agent3 import path; implementation lives in agents."""
from .agents.generator import Agent3SeleniumGenerator, find_locators

__all__ = ["Agent3SeleniumGenerator", "find_locators"]
