"""Shared TypeSafe client construction (nano-configured, swappable backend).

Every TypeSafe-backed decision in Soothe (tool-approval risk gating, intent
classification, future local replacements for LLM round trips) resolves its
endpoint the same way: nano's `classifier_providers` / `classifier` config.
Questions are bound at construction, so each decision owns its own client
instance — this module owns the shared resolution and gating logic.

Two invariants hold for every consumer:

- **Distribution-shift gate.** `Noul` answers expose only a probability (no
  confidence), so decisions that must survive out-of-distribution input use
  `Choice`, which returns a label, a distribution, and a confidence score.
  `verdict_trusted` rejects low-confidence and near-tie verdicts.
- **Unavailable is normal.** Local servers are foreground processes with no
  autostart; every failure path returns `None` so the caller degrades to its
  existing behaviour instead of blocking.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def resolve_classifier_provider(soothe_config: Any) -> tuple[str, dict[str, Any]] | None:
    """Resolve nano classifier config into `(provider_type, kwargs)`.

    Args:
        soothe_config: `SootheConfig` exposing `classifier` and
            `classifier_provider_kwargs()`.

    Returns:
        `(provider_type, kwargs)` when the classifier is enabled and its
        provider resolves; `None` when disabled, unconfigured, or unresolvable
        (callers treat `None` as "use the fallback path").
    """
    if soothe_config is None:
        return None
    try:
        cfg = soothe_config.classifier
    except AttributeError:
        return None
    if not getattr(cfg, "enabled", False):
        return None

    try:
        resolved = soothe_config.classifier_provider_kwargs()
    except Exception as exc:  # noqa: BLE001 — config resolution must not raise
        logger.warning("[classifier] provider resolution failed: %s", exc)
        return None
    if resolved is None:
        return None

    provider_type, kwargs = resolved
    if provider_type != "typesafe":
        logger.warning("[classifier] unsupported provider_type: %s", provider_type)
        return None
    return provider_type, kwargs


def build_typesafe_classifier(
    soothe_config: Any,
    questions: dict[str, Any],
) -> Any | None:
    """Build a `langchain-typesafe` classifier for the resolved provider.

    Args:
        soothe_config: `SootheConfig` providing the classifier provider.
        questions: Mapping of question id → `Noul` / `Choice` / `Score`
            question. Bound at construction (the TypeSafe API fixes questions
            per client), so each decision passes its own set.

    Returns:
        A `TypeSafeClassifier`, or `None` when the classifier is disabled,
        the provider is missing, or the optional dependency is absent.
    """
    if resolve_classifier_provider(soothe_config) is None:
        return None
    _, kwargs = resolve_classifier_provider(soothe_config) or ("", {})

    from langchain_typesafe import TypeSafeClassifier

    # The client rejects an empty key; when the provider supplies none, omit
    # the field so it falls back to TYPESAFE_API_KEY (and fails closed to
    # None if that is unset too).
    client_kwargs: dict[str, Any] = {
        "questions": questions,
        "model": kwargs.get("model") or "jev-latest",
        "timeout": kwargs.get("timeout") or 2.0,
    }
    if kwargs.get("base_url"):
        client_kwargs["base_url"] = kwargs["base_url"]
    if kwargs.get("api_key"):
        client_kwargs["api_key"] = kwargs["api_key"]
    try:
        return TypeSafeClassifier(**client_kwargs)
    except Exception as exc:  # noqa: BLE001 — missing dep / bad endpoint
        logger.warning("[classifier] backend unavailable: %s", exc)
        return None


def classifier_config(soothe_config: Any) -> Any | None:
    """Return nano's `ClassifierConfig`, or `None` when absent."""
    try:
        return soothe_config.classifier
    except AttributeError:
        return None


def verdict_trusted(cfg: Any, answer: Any) -> tuple[bool, str]:
    """Gate a `Choice` answer for distribution shift and indecision.

    Args:
        cfg: nano `ClassifierConfig` supplying `trusted()` / `decisive()`.
        answer: A `ChoiceAnswer` (or anything exposing `confidence` and
            `probabilities`).

    Returns:
        `(trusted, reason)`. Callers must fall back when `trusted` is False.
    """
    confidence = getattr(answer, "confidence", None)
    probabilities = getattr(answer, "probabilities", None)
    if cfg is not None and not cfg.trusted(confidence):
        return False, f"low confidence (drift guard): {confidence}"
    if cfg is not None and not cfg.decisive(probabilities):
        return False, f"near-tie (indecisive): {probabilities}"
    return True, ""


__all__ = [
    "build_typesafe_classifier",
    "classifier_config",
    "resolve_classifier_provider",
    "verdict_trusted",
]
