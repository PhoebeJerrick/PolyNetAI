from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, Outcome


def _clamp_binary_price(p: float) -> float:
    # Polymarket 二元市场价格通常在 (0,1)。
    # 用较小的上下界避免出现 0 导致下游风控/收益计算出现奇异行为。
    return max(0.01, min(0.99, float(p)))


def outcome_reference_price(
    features: FeatureSnapshot,
    outcome: Outcome,
    *,
    infer_missing_with_binary_complement: bool = True,
    eps: float = 1e-9,
) -> float:
    """
    返回给定 `outcome` 应使用的“该方向盘口参考价”，用于构造 `OrderIntent.reference_price`。

    重要：不可直接使用 `features.price`（它是 last_price，可能来自 up 或 down），否则
    broker 在 up/down 下单时会得到相同/错误的成交价口径。
    """

    if outcome == "up":
        px = float(getattr(features, "up_last_price", 0.0) or 0.0)
        other_px = float(getattr(features, "down_last_price", 0.0) or 0.0)
    else:
        px = float(getattr(features, "down_last_price", 0.0) or 0.0)
        other_px = float(getattr(features, "up_last_price", 0.0) or 0.0)

    if px > eps:
        return px

    if infer_missing_with_binary_complement and other_px > eps:
        return _clamp_binary_price(1.0 - other_px)

    # 兜底：保持旧行为（可能带来方向口径错误，但至少不会产生 0 奇异值）。
    fallback = float(getattr(features, "price", 0.0) or 0.0)
    return _clamp_binary_price(fallback) if fallback > eps else 0.0

