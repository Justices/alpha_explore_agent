"""Mock-only research cycle. Never executes BRAIN simulations or submissions."""
from alpha_research.research_cycle.engine import OfflineResearchCycle, OfflineSimulationGateway

__all__ = ["OfflineResearchCycle", "OfflineSimulationGateway"]
