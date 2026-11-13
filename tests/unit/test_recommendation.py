"""QA-P2-04：摘要语义分数来自候选与库画像，不是泛词命中。"""
import numpy as np
from app.research import discover


def test_semantic_scores_use_candidate_embeddings(monkeypatch):
    def fake_embed(texts):
        assert len(texts) == 3
        return np.asarray([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    monkeypatch.setattr("app.rag.embed.embed_texts", fake_embed)
    items = [{"title": "RAG", "summary": "retrieval"}, {"title": "Tennis", "summary": "next shot"}]
    scores = discover._semantic_scores("language model retrieval", items)
    assert scores == [1.0, 0.0]


def test_semantic_scores_empty():
    assert discover._semantic_scores("profile", []) == []


def test_domain_hierarchy_rejects_generic_transformer_negatives():
    assert not discover._domains("Next-shot prediction in tennis using a Transformer")
    assert not discover._domains("Pyramidal columns acting as attention heads in neuroscience")
    assert not discover._domains("Vocabulary-free next-event prediction on temporal graphs")
    assert discover._domains("Sparse low-rank adaptation for language model reasoning") == {"nlp", "systems"}
    assert "vision" in discover._domains("Vision Transformer for object recognition")
    assert discover._NEGATIVE_DOMAIN.search("Vocabulary-Free Next-Event Prediction on Temporal Graphs")
    assert discover._NEGATIVE_DOMAIN.search("Pyramidal Columns Acting as Attention Heads")
