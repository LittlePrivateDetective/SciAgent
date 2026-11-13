"""QA-P1-03 语义金标准：工程待办、研究问题与解决证据。"""
import pytest
from app.db.models import Chunk, Document
from app.research.future_work import _evidence_match, _is_research_future, _semantic_dup


@pytest.mark.parametrize("text", [
    "We intend to make the code we used to train and evaluate our models available soon.",
    "We plan to release the dataset and model checkpoints in future work.",
    "In future work we will publish our source code on GitHub.",
])
def test_engineering_todo_is_not_open_research(text):
    assert not _is_research_future(text)


@pytest.mark.parametrize("text", [
    "We leave a broader investigation of sparse attention generalization for future work.",
    "Future work should investigate whether low-rank adaptation improves cross-lingual reasoning.",
    "The robustness of retrieval under adversarial evidence remains an open question.",
    "A limitation is that our method does not yet explore multilingual alignment.",
    "We plan to release the code and investigate cross-lingual reasoning in future work.",
])
def test_research_question_is_retained(text):
    assert _is_research_future(text)


def _doc():
    return Document(id="later", filename="later.pdf", title="Sparse low-rank adaptation for reasoning",
                    abstract="We study low-rank adaptation for cross-lingual reasoning.", year="2024")


def test_resolution_requires_specific_method_result_evidence():
    idea = "Future work should investigate whether low-rank adaptation improves cross-lingual reasoning."
    good = Chunk(id="c1", doc_id="later", page=3, bbox="1,2,3,4", kind="text",
                 text="We propose sparse low-rank adaptation for cross-lingual reasoning. Results show consistent gains.")
    match = _evidence_match(idea, _doc(), [good])
    assert match and match["page"] == 3 and "We propose" in match["evidence"]


def test_generic_code_overlap_is_not_resolution():
    idea = "We intend to make the code used for model training available soon."
    doc = Document(id="x", filename="x.pdf", title="Switch Transformers", abstract="Code available", year="2022")
    generic = Chunk(id="c2", doc_id="x", page=2, bbox="1,2,3,4", kind="text",
                    text="We make our code available for model training and evaluation.")
    assert _evidence_match(idea, doc, [generic]) is None


def test_cross_version_sentence_dedup_primitive():
    a = "We leave broader evaluation of multilingual reasoning for future work."
    b = "We leave broader evaluation of multilingual reasoning for future work."
    assert _semantic_dup(a, b)
