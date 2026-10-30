"""Report package: structured graph reporting (ATM plan F-C3)."""

from know_your_soul.report.graph_report import (
    GRAPH_REPORT_KIND,
    GRAPH_REPORT_SCHEMA_VERSION,
    GraphReportError,
    QuestionError,
    answer_question,
    build_graph_analysis,
    render_graph_analysis_markdown,
)
from know_your_soul.report.report_v2 import (
    CONFIDENCE_BUCKETS,
    DEFAULT_MIN_COMMUNITY_SIZE,
    REPORT_SCHEMA_VERSION,
    ReportIntegrityError,
    build_graph_report,
    is_real_node,
    render_markdown,
)

__all__ = [
    "CONFIDENCE_BUCKETS",
    "DEFAULT_MIN_COMMUNITY_SIZE",
    "GRAPH_REPORT_KIND",
    "GRAPH_REPORT_SCHEMA_VERSION",
    "REPORT_SCHEMA_VERSION",
    "GraphReportError",
    "QuestionError",
    "ReportIntegrityError",
    "answer_question",
    "build_graph_analysis",
    "build_graph_report",
    "is_real_node",
    "render_graph_analysis_markdown",
    "render_markdown",
]
