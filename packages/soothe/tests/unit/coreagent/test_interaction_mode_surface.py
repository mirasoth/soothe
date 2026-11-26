"""Contract test: the plan/ask read-only surface must agree everywhere.

The read-only allowlist is declared once in soothe-nano
(`soothe_nano.agent.interaction_mode`) but re-declared in prose in this
repo — the decompose plan/ask addenda, the `request_plan_mode` tool
description, and the config field descriptions. If nano's surface ever
drifts (a tool added or removed), those prompts would silently lie about
what the model can actually do. These tests pin every repo-side
declaration to the installed nano constants so drift fails loudly.
"""

from __future__ import annotations

import re

from soothe_nano.agent.interaction_mode import (
    ASK_MUTATING_TOOL_GROUPS,
    ASK_POLICY_PROFILE,
    ASK_SUBAGENT_ALLOWLIST,
    ASK_SYSTEM_PROMPT_SUFFIX,
    FILESYSTEM_TOOLS_AGENT,
    FILESYSTEM_TOOLS_ASK,
    FILESYSTEM_TOOLS_PLAN,
    PLAN_POLICY_PROFILE,
    PLAN_SUBAGENT_ALLOWLIST,
    PLAN_SYSTEM_PROMPT_SUFFIX,
    ask_permissions,
    plan_permissions,
)

from soothe.prompts.fragments import ASK_MODE_ADDENDUM, PLAN_MODE_ADDENDUM

READONLY_SURFACE = {"ls", "read_file", "file_info", "glob", "grep"}
MUTATING_FS_TOOLS = set(FILESYSTEM_TOOLS_AGENT) - READONLY_SURFACE


def _backticked_tokens(text: str) -> set[str]:
    """Extract all `token` mentions from a prompt fragment."""
    return set(re.findall(r"`([a-z_]+)`", text))


def test_nano_ask_and_plan_surfaces_are_the_pinned_readonly_set() -> None:
    assert set(FILESYSTEM_TOOLS_ASK) == READONLY_SURFACE
    assert set(FILESYSTEM_TOOLS_PLAN) == READONLY_SURFACE


def test_nano_ask_and_plan_surfaces_are_read_only_subsets_of_agent() -> None:
    assert set(FILESYSTEM_TOOLS_ASK) < set(FILESYSTEM_TOOLS_AGENT)


def test_nano_mutating_groups_and_subagent_allowlists_are_pinned() -> None:
    assert ASK_MUTATING_TOOL_GROUPS == frozenset({"shell"})
    assert ASK_SUBAGENT_ALLOWLIST == frozenset({"planner"})
    assert PLAN_SUBAGENT_ALLOWLIST == frozenset()


def test_nano_policy_profiles_and_permissions_deny_writes() -> None:
    assert ASK_POLICY_PROFILE == "ask"
    assert PLAN_POLICY_PROFILE == "plan"
    for permissions in (ask_permissions(), plan_permissions()):
        write_denies = [
            p
            for p in permissions
            if "write" in getattr(p, "operations", []) and getattr(p, "mode", "") == "deny"
        ]
        assert write_denies, "plan/ask permissions must deny filesystem writes"
        assert any("/**" in getattr(p, "paths", []) for p in write_denies)


def test_decompose_addenda_advertise_exactly_the_nano_surface() -> None:
    """Both addenda must mention every read-only tool and no mutating FS tool.

    Mentions are matched as backticked tokens (`read_file`), so prose like
    "you cannot ... delete files" is not a false positive.
    """
    for name, addendum in (("ask", ASK_MODE_ADDENDUM), ("plan", PLAN_MODE_ADDENDUM)):
        tokens = _backticked_tokens(addendum)
        missing = READONLY_SURFACE - tokens
        assert not missing, f"{name}_mode_addendum does not mention {sorted(missing)}"
        leaked = MUTATING_FS_TOOLS & tokens
        assert not leaked, f"{name}_mode_addendum advertises mutating FS tools {sorted(leaked)}"


def test_nano_system_prompt_suffixes_match_the_surface() -> None:
    for suffix in (ASK_SYSTEM_PROMPT_SUFFIX, PLAN_SYSTEM_PROMPT_SUFFIX):
        tokens = _backticked_tokens(suffix)
        missing = READONLY_SURFACE - tokens
        assert not missing, f"nano mode suffix does not mention {sorted(missing)}"


def test_request_plan_mode_description_matches_the_surface() -> None:
    """The agent-mode affordance describing plan mode lists the same tools."""
    from soothe.coreagent.tools.request_plan_mode import build_request_plan_mode_tool

    description = build_request_plan_mode_tool().description
    for tool in READONLY_SURFACE:
        assert re.search(rf"\b{tool}\b", description), (
            f"request_plan_mode description omits read-only tool {tool!r}"
        )
