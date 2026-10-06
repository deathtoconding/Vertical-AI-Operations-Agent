"""Investigation: evidence becomes a grounded, schema-valid diagnosis."""

from app.investigation.collector import METRIC_PLAN, SOURCE_PLAN, EvidenceCollector
from app.investigation.context import build_investigation_prompt, rank_evidence, select_evidence
from app.investigation.investigator import Investigator

__all__ = [
    "METRIC_PLAN",
    "SOURCE_PLAN",
    "EvidenceCollector",
    "Investigator",
    "build_investigation_prompt",
    "rank_evidence",
    "select_evidence",
]
