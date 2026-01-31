"""Embedding service for generating and comparing media embeddings."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Thread pool for CPU-intensive embedding operations
# This prevents blocking the asyncio event loop
_embedding_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embedding")

# Lazy load sentence-transformers to avoid startup time impact
_model = None


def _get_model():
    """Lazy load the sentence transformer model."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading sentence-transformers model (all-MiniLM-L6-v2)...")
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Model loaded successfully")
        except ImportError:
            logger.error("sentence-transformers not installed. Run: pip install sentence-transformers")
            raise
    return _model


class EmbeddingService:
    """Service for generating and comparing media embeddings.

    Uses the all-MiniLM-L6-v2 model which is:
    - Fast (~14k sentences/sec on CPU)
    - Small (~80MB)
    - Good quality for semantic similarity
    """

    EMBEDDING_DIM = 384  # Dimension of all-MiniLM-L6-v2 embeddings

    @staticmethod
    def create_media_text(
        title: str,
        description: str | None = None,
        genres: list[str] | None = None,
        authors: list[str] | None = None,
        keywords: list[str] | None = None,
        year: int | None = None,
    ) -> str:
        """Create a text representation of media for embedding.

        Combines multiple fields into a single text that captures
        the semantic meaning of the media.
        """
        parts = [title]

        if year:
            parts.append(f"({year})")

        if authors:
            parts.append(f"by {', '.join(authors)}")

        if genres:
            parts.append(f"Genres: {', '.join(genres)}")

        if keywords:
            parts.append(f"Keywords: {', '.join(keywords[:10])}")  # Limit keywords

        if description:
            # Truncate description to ~500 chars for efficiency
            desc = description[:500] + "..." if len(description) > 500 else description
            parts.append(desc)

        return " | ".join(parts)

    @classmethod
    def generate_embedding(cls, text: str) -> list[float]:
        """Generate embedding for a single text (sync version).

        Args:
            text: Text to embed

        Returns:
            List of floats representing the embedding vector
        """
        model = _get_model()
        embedding = model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return embedding.tolist()

    @classmethod
    async def generate_embedding_async(cls, text: str) -> list[float]:
        """Generate embedding for a single text (async version).

        Runs in thread pool to avoid blocking the event loop.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_embedding_executor, cls.generate_embedding, text)

    @classmethod
    def generate_embeddings_batch(cls, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in batch (sync version).

        More efficient than calling generate_embedding multiple times.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        model = _get_model()
        embeddings = model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 100,
            batch_size=32,
        )
        return [emb.tolist() for emb in embeddings]

    @classmethod
    async def generate_embeddings_batch_async(cls, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in batch (async version).

        Runs in thread pool to avoid blocking the event loop.
        """
        if not texts:
            return []
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_embedding_executor, cls.generate_embeddings_batch, texts)

    @staticmethod
    def cosine_similarity(embedding1: list[float], embedding2: list[float]) -> float:
        """Calculate cosine similarity between two embeddings.

        Since embeddings are normalized, this is equivalent to dot product.
        """
        if np is None:
            raise ImportError("numpy is required for recommendations. Install with: pip install -e '.[ml]'")
        arr1 = np.array(embedding1)
        arr2 = np.array(embedding2)
        return float(np.dot(arr1, arr2))

    @classmethod
    def find_similar(
        cls,
        query_embedding: list[float],
        candidate_embeddings: list[tuple[int, list[float]]],  # List of (id, embedding)
        top_k: int = 10,
        min_similarity: float = 0.3,
    ) -> list[tuple[int, float]]:
        """Find most similar items to query embedding.

        Args:
            query_embedding: The embedding to compare against
            candidate_embeddings: List of (id, embedding) tuples
            top_k: Number of results to return
            min_similarity: Minimum similarity threshold

        Returns:
            List of (id, similarity_score) tuples, sorted by score descending
        """
        if not candidate_embeddings:
            return []

        if np is None:
            raise ImportError("numpy is required for recommendations. Install with: pip install -e '.[ml]'")
        query = np.array(query_embedding)
        results = []

        for item_id, emb in candidate_embeddings:
            similarity = float(np.dot(query, np.array(emb)))
            if similarity >= min_similarity:
                results.append((item_id, similarity))

        # Sort by similarity descending and return top_k
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    @classmethod
    def compute_user_profile_embedding(
        cls,
        media_embeddings: list[tuple[list[float], float]],  # List of (embedding, rating)
    ) -> list[float] | None:
        """Compute a user profile embedding based on rated media.

        Creates a weighted average of media embeddings, where weights
        are based on user ratings. Higher rated media has more influence.

        Args:
            media_embeddings: List of (embedding, rating) tuples

        Returns:
            User profile embedding or None if no data
        """
        if not media_embeddings:
            return None

        # Convert ratings to weights (normalize to 0-1 range, then shift so even low ratings contribute)
        # Rating 1 -> weight 0.2, Rating 5 -> weight 1.0
        embeddings = []
        weights = []

        if np is None:
            raise ImportError("numpy is required for recommendations. Install with: pip install -e '.[ml]'")
        for emb, rating in media_embeddings:
            if emb:
                embeddings.append(np.array(emb))
                # Transform rating to weight: (rating - 1) / 4 * 0.8 + 0.2
                weight = (rating - 1) / 4 * 0.8 + 0.2 if rating else 0.5
                weights.append(weight)

        if not embeddings:
            return None

        # Weighted average
        weights = np.array(weights)
        weights = weights / weights.sum()  # Normalize weights
        embeddings = np.array(embeddings)

        profile = np.average(embeddings, axis=0, weights=weights)
        # Normalize the profile embedding
        profile = profile / np.linalg.norm(profile)

        return profile.tolist()
