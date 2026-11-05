"""Structured question schema shared by the `ask_user` tool and the relay.

Pure pydantic models with no heavy runtime imports, so low-level packages
(`context.decomposition`) can import them without triggering the
`coreagent` / `sloop.middleware` dependency chain.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OptionSpec(BaseModel):
    """A single selectable option for a structured question."""

    label: str = Field(description="Display text (1-5 words). The user selects this.")
    description: str = Field(description="What this option means or what happens if chosen.")

    model_config = ConfigDict(extra="forbid")


class QuestionSpec(BaseModel):
    """A structured question with 2-4 options."""

    question: str = Field(description="The full question text. Clear, specific, ends with '?'.")
    header: str = Field(
        description="Short chip label (max 12 chars). E.g. 'Auth method'. Truncated if longer."
    )
    options: list[OptionSpec] = Field(
        description="2-4 options. Put the recommended one first with '(Recommended)' in its label. Never include an 'Other' option — it's auto-added.",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _coerce(self) -> QuestionSpec:
        """Coerce LLM-produced values: truncate header/question, pad/trim options, dedup labels."""
        # Header: truncate to 12 chars.
        if len(self.header) > 12:
            self.header = self.header[:12]

        # Question: truncate to 100 words.
        words = self.question.split()
        if len(words) > 100:
            self.question = " ".join(words[:100])

        # Options: ensure 2-4.
        if len(self.options) > 4:
            self.options = self.options[:4]
        while len(self.options) < 2:
            self.options.append(
                OptionSpec(
                    label=f"Option {len(self.options) + 1}",
                    description="No description provided.",
                )
            )

        # Option labels: ensure non-empty and unique.
        seen: set[str] = set()
        for i, opt in enumerate(self.options):
            if not opt.label.strip():
                opt.label = f"Option {i + 1}"
            base = opt.label
            suffix = 1
            while opt.label in seen:
                suffix += 1
                opt.label = f"{base} ({suffix})"
            seen.add(opt.label)
            if not opt.description.strip():
                opt.description = "No description provided."
        return self


__all__ = ["OptionSpec", "QuestionSpec"]
