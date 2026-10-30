"""Calibrated-probability decision model for the veritas fast path.

The classifier is a built-in fast path inside the veritas answerer — Jev
first, LLM second — rather than a separate middleware wiring.

The classifier picks one of three labels — `allow`, `escalate`, `reject` —
for a subject. Backends are configured in nano (`providers` entries with
`provider_type: typesafe`, plus the shared `classifier` parameters); this
module only binds a resolved provider to the TypeSafe wire protocol via
`langchain-typesafe`.

Why a categorical (`Choice`) question rather than a binary (`Noul`) one:
`Noul` answers expose only a probability — the API documents that they carry
no confidence — so out-of-distribution input cannot be detected. `Choice`
returns the selected label, the full probability distribution, *and* a
confidence score, which is exactly what the two gates below need.

Two properties the gate depends on:

- **Distribution-shift gate.** A calibrated verdict is only meaningful inside
  the endpoint's training distribution; out-of-distribution input comes back
  with a diffuse distribution (low confidence) or a near-tie between labels.
  Both are treated as unusable and the caller falls back.
- **Unavailable is normal.** Local classifier servers are foreground
  processes with no autostart, so every failure path (missing provider,
  unresolved env, import failure, timeout, transport error) resolves to
  `unavailable` and the caller falls back instead of blocking.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_QUESTION_ID = "risk_decision"
_ALLOW = "allow"
_ESCALATE = "escalate"
_REJECT = "reject"

# HTTP statuses that indicate a permanently unavailable backend (legal block,
# auth failure, gone resource). On any of these the classifier circuit-trips
# so subsequent calls short-circuit to `unavailable` instead of retrying the
# endpoint on every tool call (loop a47a: 75 redundant 451 round-trips).
_PERMANENT_HTTP_STATUSES = frozenset({401, 403, 404, 410, 451})

# Matches a 3-digit HTTP status code in an exception message (fallback when
# the exception lacks a structured status-code attribute).
_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")

_INSTRUCTIONS = (
    "Decide how this tool call should be handled for the stated goal: allow it, "
    "escalate it for human review, or reject it."
)

_CRITERIA: dict[str, str] = {
    _ALLOW: (
        "Routine and reversible, confined to the workspace, and directly "
        "implied by the user's request."
    ),
    _ESCALATE: ("Plausible but consequential, or ambiguous enough that a human should decide."),
    _REJECT: (
        "Destructive or irreversible, outside the workspace, "
        "privilege-escalating, or unrelated to the user's request."
    ),
}


@dataclass(frozen=True)
class RiskQuery:
    """One subject to classify (a tool call, in the current integration)."""

    tool: str
    args_preview: dict[str, Any]
    """Redacted, bounded argument preview — never file contents or secrets."""

    goal_summary: str | None = None


@dataclass(frozen=True)
class RiskVerdict:
    """Classifier result for one subject."""

    band: str
    """`allow` / `escalate` / `reject`, or `untrusted` / `unavailable`."""

    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        """True when the verdict may influence a decision."""
        return self.band in (_ALLOW, _ESCALATE, _REJECT)


class RiskClassifier(Protocol):
    """Backend-agnostic risk classification."""

    async def classify(self, items: list[RiskQuery]) -> list[RiskVerdict]:
        """Classify subjects, returning one verdict per input in order."""
        ...


class NullRiskClassifier:
    """Unavailable backend: every verdict is `unavailable` (caller falls back)."""

    def __init__(self, reason: str = "classifier disabled") -> None:
        self._reason = reason

    async def classify(self, items: list[RiskQuery]) -> list[RiskVerdict]:
        """Return unavailable verdicts for every input."""
        return [RiskVerdict(band="unavailable", reason=self._reason) for _ in items]


def _extract_http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status code from a transport/HTTP exception.

    Checks structured attributes (``response.status_code``, ``status_code``)
    before falling back to a regex on the message. Returns ``None`` when no
    status can be determined.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_RE.search(str(exc))
    if match is not None:
        return int(match.group(1))
    return None


class TypeSafeRiskClassifier:
    """TypeSafe `/v1/systemone` backend via `langchain-typesafe`."""

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str,
        timeout: float,
        classifier_config: Any,
        max_states_per_request: int = 32,
    ) -> None:
        """Build the langchain classifier for a resolved provider.

        Args:
            base_url: TypeSafe endpoint base URL (optional).
            api_key: Endpoint credential; omitted when the provider supplies
                none so the client falls back to `TYPESAFE_API_KEY` (and
                fails closed to the null backend when unset).
            model: Classifier model name (e.g. `jev-latest`).
            timeout: Per-request timeout in seconds.
            classifier_config: Nano `ClassifierConfig` supplying the trust /
                decisiveness gates.
            max_states_per_request: Batch bound for `abatch` calls.
        """
        from langchain_typesafe import Choice, TypeSafeClassifier

        self._config = classifier_config
        self._max_states = max(1, int(max_states_per_request))
        # Questions travel per-request in langchain-typesafe >=0.0.1a3; hold
        # the shared question set and bind it into each ClassifierRequest.
        self._questions = {_QUESTION_ID: Choice(instructions=_INSTRUCTIONS, criteria=_CRITERIA)}
        # The client rejects an empty key; when the provider supplies none,
        # omit the field so it falls back to TYPESAFE_API_KEY (and fails
        # closed to the null backend if that is unset too).
        client_kwargs: dict[str, Any] = {
            "model": model,
            "timeout": timeout,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        if api_key:
            client_kwargs["api_key"] = api_key
        self._classifier = TypeSafeClassifier(**client_kwargs)
        # Circuit breaker: once a permanent HTTP failure (e.g. 451 legal
        # block) is observed, short-circuit every subsequent call to an
        # `unavailable` verdict so the endpoint is not retried per tool call.
        self._circuit_tripped: bool = False
        self._circuit_reason: str | None = None

    def _state(self, item: RiskQuery) -> dict[str, Any]:
        state: dict[str, Any] = {"tool": item.tool, "args": item.args_preview}
        if item.goal_summary:
            state["goal"] = item.goal_summary
        return state

    def _verdict(self, answer: Any) -> RiskVerdict:
        """Map a Choice answer through the trust and margin gates."""
        probabilities = dict(getattr(answer, "probabilities", None) or {})
        confidence = getattr(answer, "confidence", None)
        choice = str(getattr(answer, "choice", "") or "")
        detail = f"choice={choice} confidence={confidence} probs={probabilities}"
        if not self._config.trusted(confidence):
            return RiskVerdict(
                band="untrusted",
                confidence=confidence,
                probabilities=probabilities,
                reason=f"low confidence (drift guard): {detail}",
            )
        if not self._config.decisive(probabilities):
            return RiskVerdict(
                band="untrusted",
                confidence=confidence,
                probabilities=probabilities,
                reason=f"near-tie (indecisive): {detail}",
            )
        if choice not in (_ALLOW, _ESCALATE, _REJECT):
            return RiskVerdict(
                band="unavailable",
                confidence=confidence,
                probabilities=probabilities,
                reason=f"unknown label: {detail}",
            )
        return RiskVerdict(
            band=choice,
            confidence=confidence,
            probabilities=probabilities,
            reason=detail,
        )

    async def classify(self, items: list[RiskQuery]) -> list[RiskVerdict]:
        """Classify subjects in bounded batches; failures degrade to fallback.

        A permanent HTTP failure (e.g. 451 legal block) trips a circuit
        breaker so subsequent calls return ``unavailable`` verdicts without
        hitting the endpoint — the backend is unavailable for the rest of
        the run, not retried per tool call.
        """
        from langchain_typesafe.types import ClassifierRequest

        if not items:
            return []
        if self._circuit_tripped:
            return [
                RiskVerdict(
                    band="unavailable",
                    reason=f"classifier circuit-open: {self._circuit_reason}",
                )
                for _ in items
            ]
        verdicts: list[RiskVerdict] = []
        for start in range(0, len(items), self._max_states):
            batch = items[start : start + self._max_states]
            try:
                responses = await self._classifier.abatch(
                    [
                        ClassifierRequest(state=self._state(item), questions=self._questions)
                        for item in batch
                    ]
                )
            except Exception as exc:  # noqa: BLE001 — never block on the classifier
                status = _extract_http_status(exc)
                if status in _PERMANENT_HTTP_STATUSES:
                    self._circuit_tripped = True
                    self._circuit_reason = f"HTTP {status}"
                    logger.warning(
                        "[decision_model] backend permanently unavailable "
                        "(HTTP %s); circuit tripped — subsequent calls short-circuit",
                        status,
                    )
                else:
                    logger.warning(
                        "[decision_model] classification unavailable (%s); falling back",
                        exc,
                    )
                verdicts.extend(
                    RiskVerdict(band="unavailable", reason=f"classifier error: {exc}")
                    for _ in batch
                )
                continue
            answers = [getattr(response, "answers", {}).get(_QUESTION_ID) for response in responses]
            # A server returning fewer responses than inputs is a defect; pad
            # so every subject still gets a verdict (never a silent allow).
            while len(answers) < len(batch):
                answers.append(None)
            for answer in answers[: len(batch)]:
                if answer is None:
                    verdicts.append(RiskVerdict(band="unavailable", reason="no choice answer"))
                    continue
                verdicts.append(self._verdict(answer))
        return verdicts


def build_risk_classifier(soothe_config: Any) -> RiskClassifier:
    """Resolve nano config into a classifier, or a null backend.

    Args:
        soothe_config: `SootheConfig` exposing `classifier` and
            `classifier_provider_kwargs()`.

    Returns:
        A `TypeSafeRiskClassifier` when enabled and resolvable; otherwise a
        `NullRiskClassifier` (the caller's deterministic path stays intact).
    """
    from soothe.sloop.clarification.typesafe_client import (
        classifier_config,
        resolve_classifier_provider,
    )

    cfg = classifier_config(soothe_config)
    if cfg is None:
        return NullRiskClassifier("no classifier config")
    if not cfg.enabled:
        return NullRiskClassifier("classifier disabled")

    resolved = resolve_classifier_provider(soothe_config)
    if resolved is None:
        return NullRiskClassifier("provider unavailable")
    _provider_type, kwargs = resolved

    try:
        return TypeSafeRiskClassifier(
            base_url=kwargs.get("base_url"),
            api_key=kwargs.get("api_key"),
            model=kwargs.get("model") or "jev-latest",
            timeout=kwargs.get("timeout") or 2.0,
            classifier_config=cfg,
            max_states_per_request=kwargs.get("max_states_per_request") or 32,
        )
    except Exception as exc:  # noqa: BLE001 — missing dep / bad endpoint
        logger.warning("[decision_model] backend unavailable: %s", exc)
        return NullRiskClassifier(f"backend unavailable: {exc}")


__all__ = [
    "NullRiskClassifier",
    "RiskClassifier",
    "RiskQuery",
    "RiskVerdict",
    "TypeSafeRiskClassifier",
    "build_risk_classifier",
]
