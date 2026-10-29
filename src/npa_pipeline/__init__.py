"""npa-pipeline: поиск и скачивание официальных нормативных правовых актов."""

from npa_pipeline.models import MatchType, Query, Result, Status
from npa_pipeline.parse_citation import ParsedCitation, parse_citation, query_from_citation
from npa_pipeline.service import fetch_document

__all__ = [
    "MatchType",
    "ParsedCitation",
    "Query",
    "Result",
    "Status",
    "fetch_document",
    "parse_citation",
    "query_from_citation",
]
