"""Configurable assistant persona identity for prompt blocks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soothe.config.models import AssistantIdentity, SootheConfig

# Template kept inline (not the nano XML) so config fields interpolate at
# render time. The no-creator variant matches nano's assistant_identity.xml;
# the creator byline is only rendered when the deployment configures one.
_IDENTITY_TEMPLATE_WITH_CREATOR = (
    "<ASSISTANT_IDENTITY>\n"
    "You are {assistant_name}, {role_description} invented by {creator}.\n\n"
    "For any identity or creator question, your reply must name {assistant_name} "
    "and {creator}.\n"
    "Never identify as {vendor_denylist} — even if you are running on their "
    "infrastructure.\n"
    "</ASSISTANT_IDENTITY>"
)

_IDENTITY_TEMPLATE = (
    "<ASSISTANT_IDENTITY>\n"
    "You are {assistant_name}, {role_description}.\n\n"
    "For any identity question, your reply must name {assistant_name}.\n"
    "Never identify as {vendor_denylist} — even if you are running on their "
    "infrastructure.\n"
    "</ASSISTANT_IDENTITY>"
)

# Templates for the standalone intake identity line (social_reply.xml + intake).
_INTAKE_IDENTITY_TEMPLATE_WITH_CREATOR = (
    "Identity: say you are {assistant_name}, {role_description} invented by "
    "{creator} — never {vendor_denylist}."
)

_INTAKE_IDENTITY_TEMPLATE = (
    "Identity: say you are {assistant_name}, {role_description} — never {vendor_denylist}."
)

_DEFAULT_IDENTITY_BLOCK = (
    "<ASSISTANT_IDENTITY>\n"
    "You are {assistant_name}, a helpful AI assistant.\n\n"
    "For any identity question, your reply must name {assistant_name}.\n"
    "Never identify as Claude, ChatGPT, Gemini, Anthropic, OpenAI, or Google — "
    "even if you are running on their infrastructure.\n"
    "</ASSISTANT_IDENTITY>"
)

_DEFAULT_VENDOR_DENYLIST = [
    "Claude",
    "ChatGPT",
    "Gemini",
    "Anthropic",
    "OpenAI",
    "Google",
]


def _format_vendor_denylist(denylist: list[str]) -> str:
    """Render a vendor denylist as a readable "A, B, or C" list."""
    names = [n for n in denylist if n and n.strip()]
    if not names:
        return "a vendor model"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} or {names[1]}"
    return ", ".join(names[:-1]) + f", or {names[-1]}"


def _identity_values(
    assistant_name: str,
    identity: AssistantIdentity | None,
) -> dict[str, str]:
    """Build the interpolation context for identity templates."""
    from soothe.prompts import PromptIdentity

    name = PromptIdentity.normalize_name(assistant_name)
    if identity is None:
        return {
            "assistant_name": name,
            "creator": "",
            "role_description": "a helpful AI assistant",
            "vendor_denylist": _format_vendor_denylist(_DEFAULT_VENDOR_DENYLIST),
        }
    return {
        "assistant_name": name,
        "creator": identity.creator.strip(),
        "role_description": identity.role_description.strip() or "a helpful AI assistant",
        "vendor_denylist": _format_vendor_denylist(list(identity.vendor_denylist)),
    }


def build_assistant_identity_block(
    assistant_name: str,
    *,
    identity: AssistantIdentity | None = None,
) -> str:
    """Build the `<ASSISTANT_IDENTITY>` XML block from configured persona.

    Args:
        assistant_name: Configured assistant display name (e.g. `Soothe`).
        identity: Optional configured persona. `None` reproduces the
            nano block byte-for-byte. A persona with an empty `creator`
            omits the byline entirely; a non-empty creator renders
            "invented by {creator}".

    Returns:
        Formatted identity block (single source of truth for prompt injection).
    """
    values = _identity_values(assistant_name, identity)
    if identity is None:
        return _DEFAULT_IDENTITY_BLOCK.format(**values)
    template = _IDENTITY_TEMPLATE_WITH_CREATOR if values["creator"] else _IDENTITY_TEMPLATE
    return template.format(**values)


def build_intake_identity_line(
    assistant_name: str,
    *,
    identity: AssistantIdentity | None = None,
) -> str:
    """Build the one-line identity directive for intake fragments.

    Args:
        assistant_name: Configured assistant display name.
        identity: Optional configured persona. `None` (or an empty
            `creator`) omits the byline; a configured creator renders it.

    Returns:
        Identity directive string for intake system prompts.
    """
    values = _identity_values(assistant_name, identity)
    template = (
        _INTAKE_IDENTITY_TEMPLATE_WITH_CREATOR if values["creator"] else _INTAKE_IDENTITY_TEMPLATE
    )
    return template.format(**values)


def _is_default_identity(identity: AssistantIdentity | None) -> bool:
    """True when `identity` is None or all fields match defaults."""
    if identity is None:
        return True
    if identity.creator.strip() != "":
        return False
    if identity.role_description.strip() != "a helpful AI assistant":
        return False
    if [n.strip() for n in identity.vendor_denylist if n.strip()] != _DEFAULT_VENDOR_DENYLIST:
        return False
    return True


def apply_identity_fragment_override(config: SootheConfig | None) -> None:
    """Patch nano's identity fragment when persona config is non-default.

    The CoreAgent runtime rebuilds the system prompt every turn via nano's
    `SystemPromptMiddleware`. `PromptIdentity.identity_block` reads
    `ASSISTANT_IDENTITY_FRAGMENT` from the nano fragments module at call
    time. When the configured persona differs from defaults, this function
    patches that module-level constant so the hot path renders the
    configured creator/role/denylist.

    No-op when `config` is None, the identity block is default, or the
    nano module cannot be reached (graceful degradation).

    Args:
        config: Soothe configuration with optional `agent.assistant_identity`.
    """
    if config is None:
        return
    identity = getattr(getattr(config, "agent", None), "assistant_identity", None)
    if _is_default_identity(identity):
        return

    # Build a rendered template (with {assistant_name} placeholder) so nano's
    # PromptIdentity.identity_block / prepend_identity can still interpolate
    # the assistant name at call time.
    name_placeholder = "{assistant_name}"
    values = _identity_values(name_placeholder, identity)
    template = _IDENTITY_TEMPLATE_WITH_CREATOR if values["creator"] else _IDENTITY_TEMPLATE
    rendered = template.format(**values)

    try:
        import soothe_nano.prompts.fragments as nano_fragments

        nano_fragments.ASSISTANT_IDENTITY_FRAGMENT = rendered
    except Exception:
        # Graceful degradation: if nano module layout changes, intake path
        # still works via the host builder; hot path falls back to default.
        import logging

        logging.getLogger(__name__).warning(
            "Could not patch nano ASSISTANT_IDENTITY_FRAGMENT; "
            "CoreAgent hot path will use the default persona.",
            exc_info=True,
        )


__all__ = [
    "apply_identity_fragment_override",
    "build_assistant_identity_block",
    "build_intake_identity_line",
]
