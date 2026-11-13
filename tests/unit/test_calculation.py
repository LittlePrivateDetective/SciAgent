from app.agents.calculation import lora_square_answer


def test_lora_user_values_are_calculated():
    q = "依据 LoRA 定义：若 d_model=512、r=8，对单个权重矩阵，A/B 形状和可训练参数共有多少？"
    ev = [{"text": "W0 + ΔW = W0 + BA, where B ∈ R^{d×r}, A ∈ R^{r×k}"}]
    out = lora_square_answer(q, ev)
    assert out and "8×512" in out and "512×8" in out and "8192" in out


def test_lora_missing_or_invalid_conditions_stay_unanswered():
    q = "LoRA 的 d_model=512 时参数是多少？"
    assert lora_square_answer(q, []) is None
    assert lora_square_answer("Transformer 有多少层？", []) is None
    assert lora_square_answer("LoRA d_model=512, r=1024 的参数", []) is None
