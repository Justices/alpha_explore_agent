"""Configurable research-agent runtime.

Agent roles are domain-level capabilities; model vendors and transports are replaceable.
No adapter in this package is authorized to submit a BRAIN simulation or Alpha.
"""
from alpha_research.agents.router import RoleRoutedModelClient

__all__ = ["RoleRoutedModelClient"]
