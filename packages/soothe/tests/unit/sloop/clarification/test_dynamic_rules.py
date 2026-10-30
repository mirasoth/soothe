"""Unit tests for the loop-scoped dynamic-rule layer (RFC-LOOP-030)."""

from __future__ import annotations

from soothe.sloop.clarification.dynamic_rules import (
    DEFAULT_GRANT_CAP,
    LoopGrantStore,
    command_prefix,
    grant_hit,
)


class TestCommandPrefix:
    def test_first_token(self) -> None:
        assert command_prefix("pytest -xvs tests/") == "pytest"

    def test_compound_takes_first_token_only(self) -> None:
        assert command_prefix("cd /x && brew install jq") == "cd"

    def test_empty(self) -> None:
        assert command_prefix("") is None
        assert command_prefix("   ") is None


class TestGrantHit:
    def test_signature_record_matches_exact(self) -> None:
        recs = [{"tool": "run_command", "signature": "rm -rf /"}]
        assert grant_hit("run_command", {"command": "rm -rf /"}, recs) is True
        assert grant_hit("run_command", {"command": "rm -rf /etc"}, recs) is False

    def test_prefix_record_matches_same_first_token(self) -> None:
        recs = [{"tool": "run_command", "prefix": "pytest", "source": "decision_model"}]
        assert grant_hit("run_command", {"command": "pytest -xvs a"}, recs) is True
        assert grant_hit("run_command", {"command": "pytest"}, recs) is True
        assert grant_hit("run_command", {"command": "ruff check ."}, recs) is False

    def test_prefix_only_for_command_tools(self) -> None:
        """Path tools never prefix-match: an edit_file signature is exact."""
        recs = [{"tool": "edit_file", "prefix": "/workspace"}]
        assert grant_hit("edit_file", {"file_path": "/workspace/other.py"}, recs) is False

    def test_rule_family_record(self) -> None:
        recs = [{"rule": "command.dangerous.rm_root"}]
        assert grant_hit(
            "run_command", {"command": "rm -rf /x"}, recs, rule_id="command.dangerous.rm_rf"
        )
        assert not grant_hit(
            "run_command",
            {"command": "rm -rf /x"},
            recs,
            rule_id="command.dangerous.git_force_push",
        )

    def test_rule_record_ignored_without_rule_id(self) -> None:
        """A rule record alone cannot approve anything at stage 2 (the rule
        id is only known at the safety stage)."""
        recs = [{"rule": "command.dangerous.rm_root"}]
        assert grant_hit("run_command", {"command": "rm -rf /"}, recs) is False

    def test_empty_records(self) -> None:
        assert grant_hit("run_command", {"command": "x"}, None) is False
        assert grant_hit("run_command", {"command": "x"}, []) is False


class TestLoopGrantStore:
    def test_record_run_command_mints_prefix(self) -> None:
        store = LoopGrantStore()
        rec = store.record(
            "run_command", {"command": "pytest -xvs tests/"}, source="decision_model"
        )
        assert rec == {"tool": "run_command", "prefix": "pytest", "source": "decision_model"}
        assert store.matches("run_command", {"command": "pytest -q"}, None) is True

    def test_record_path_tool_mints_signature(self) -> None:
        store = LoopGrantStore()
        rec = store.record("edit_file", {"file_path": "/w/a.py"}, source="llm")
        assert rec == {"tool": "edit_file", "signature": "/w/a.py", "source": "llm"}
        assert store.matches("edit_file", {"file_path": "/w/a.py"}, None) is True
        assert store.matches("edit_file", {"file_path": "/w/b.py"}, None) is False

    def test_record_rule_override(self) -> None:
        store = LoopGrantStore()
        store.record(
            "run_command",
            {"command": "anything"},
            source="human",
            rule_id="command.dangerous.rm_root",
        )
        assert store.matches(
            "run_command", {"command": "rm -rf /x"}, None, rule_id="command.dangerous.rm_rf"
        )

    def test_record_unsignable_tool_returns_none(self) -> None:
        store = LoopGrantStore()
        assert store.record("mcp_tool", {"foo": "bar"}, source="llm") is None

    def test_dedup(self) -> None:
        store = LoopGrantStore()
        store.record("run_command", {"command": "pytest a"}, source="decision_model")
        store.record("run_command", {"command": "pytest b"}, source="decision_model")
        assert len(store.records) == 1

    def test_cap_evicts_oldest(self) -> None:
        store = LoopGrantStore(cap=2)
        store.record("run_command", {"command": "pytest a"}, source="decision_model")
        store.record("run_command", {"command": "ruff b"}, source="decision_model")
        store.record("run_command", {"command": "mypy c"}, source="decision_model")
        assert len(store.records) == 2
        assert store.matches("run_command", {"command": "pytest a"}, None) is False
        assert store.matches("run_command", {"command": "mypy c"}, None) is True

    def test_matches_merges_instance_store_and_allowlist(self) -> None:
        """Human approvals (graph state) and gate grants (instance) both clear."""
        store = LoopGrantStore()
        allowlist = [{"tool": "run_command", "signature": "git status"}]
        assert store.matches("run_command", {"command": "git status"}, allowlist) is True
        store.record("run_command", {"command": "pytest a"}, source="decision_model")
        assert store.matches("run_command", {"command": "pytest b"}, allowlist) is True

    def test_default_cap(self) -> None:
        assert DEFAULT_GRANT_CAP == 256
        store = LoopGrantStore()
        for i in range(DEFAULT_GRANT_CAP + 10):
            store.record("run_command", {"command": f"cmd{i}"}, source="decision_model")
        assert len(store.records) == DEFAULT_GRANT_CAP


class TestGrantPipelineIntegration:
    def test_pipeline_honors_prefix_grant_at_stage_2(self) -> None:
        """A prefix grant clears at the allowlist stage — before safety even
        runs (zero model calls, zero hints)."""
        from soothe.config.models import ToolApprovalConfig
        from soothe.sloop.clarification.tool_approval_pipeline import ToolApprovalPipeline

        pipeline = ToolApprovalPipeline(ToolApprovalConfig())
        grant = [{"tool": "run_command", "prefix": "git", "source": "decision_model"}]
        result = pipeline.evaluate_action(
            "run_command",
            {"command": "git push -f origin main"},
            allowlist=grant,
        )
        assert result.decision == "approve"
        assert result.stage == "allowlist"
        assert result.risk_hints == ()

    def test_human_rule_record_suppresses_hint(self) -> None:
        """A prior human approval of the rm family suppresses the rm_rf hint."""
        from soothe.config.models import ToolApprovalConfig
        from soothe.sloop.clarification.tool_approval_pipeline import ToolApprovalPipeline

        pipeline = ToolApprovalPipeline(ToolApprovalConfig())
        result = pipeline.evaluate_action(
            "run_command",
            {"command": "rm -rf build/"},
            allowlist=[{"rule": "command.dangerous.rm_root"}],
        )
        assert result.decision == "approve"
        assert result.stage == "allowlist"
