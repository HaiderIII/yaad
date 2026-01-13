"""Recommendation services package."""

from src.services.recommendations.embeddings import EmbeddingService
from src.services.recommendations.engine import ProgressEvent, RecommendationEngine

__all__ = ["RecommendationEngine", "EmbeddingService", "ProgressEvent"]
