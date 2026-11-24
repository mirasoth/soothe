"""Unit tests for the email channel Authentication-Results (A-R) handling.

Only the receiver's own authserv-id is honored; sender-forged A-R headers must
count as absent so they can never grant an SPF/DKIM pass.
"""

from __future__ import annotations

from types import SimpleNamespace

from soothe_daemon.channels.email import EmailChannel, EmailConfig

TRUSTED = {"example.com", "smtp.example.com"}


def _msg_with_ar(*headers: str) -> SimpleNamespace:
    """Build a parsed-message double carrying the given A-R headers."""
    return SimpleNamespace(
        get_all=lambda name: list(headers) if name == "Authentication-Results" else []
    )


def _check(*headers: str, trusted: set[str] = TRUSTED) -> tuple[bool, bool]:
    return EmailChannel._check_authentication_results(_msg_with_ar(*headers), trusted)


class TestAuthenticationResults:
    def test_forged_foreign_authserv_id_is_ignored(self):
        spf, dkim = _check(
            "mail.attacker.net; spf=pass smtp.mailfrom=attacker.net; dkim=pass header.d=attacker.net"
        )
        assert (spf, dkim) == (False, False)

    def test_own_authserv_id_is_honored(self):
        spf, dkim = _check(
            "mx.example.com; spf=pass smtp.mailfrom=partner.example; dkim=pass header.d=partner.example"
        )
        assert (spf, dkim) == (True, True)

    def test_exact_trusted_token_is_honored(self):
        spf, dkim = _check("example.com; spf=pass; dkim=pass")
        assert (spf, dkim) == (True, True)

    def test_lookalike_authserv_id_is_ignored(self):
        spf, dkim = _check("notexample.com; spf=pass; dkim=pass")
        assert (spf, dkim) == (False, False)

    def test_verdicts_are_independent(self):
        spf, dkim = _check(
            "mx.example.com; spf=pass; dkim=fail",
            "mx.example.com; spf=fail; dkim=pass",
        )
        assert (spf, dkim) == (True, True)

    def test_no_trusted_ids_rejects_all_headers(self):
        spf, dkim = _check(
            "mx.example.com; spf=pass; dkim=pass",
            trusted=set(),
        )
        assert (spf, dkim) == (False, False)

    def test_missing_header_is_absent(self):
        spf, dkim = _check()
        assert (spf, dkim) == (False, False)


class TestReceivedHeaderIdentity:
    """The receiving MTA from the topmost Received header is trusted."""

    def _channel(self) -> EmailChannel:
        return EmailChannel(EmailConfig(from_address="user@example.com"), manager=None)

    def _msg(self, *ar: str, received: list[str] | None = None) -> SimpleNamespace:
        def get_all(name):
            if name == "Authentication-Results":
                return list(ar)
            if name == "Received":
                return list(received or [])
            return []

        return SimpleNamespace(get_all=get_all)

    def test_topmost_received_host_is_trusted(self):
        channel = self._channel()
        parsed = self._msg(
            "mx.google.com; spf=pass; dkim=pass",
            received=[
                "by mx.google.com with SMTP id abc; Tue, 24 Nov 2026 00:00:00 +0000",
                "by mail.attacker.net; Tue, 24 Nov 2026 00:00:00 +0000",
            ],
        )
        trusted = channel._message_trusted_authserv_ids(parsed)
        assert "mx.google.com" in trusted
        assert "mail.attacker.net" not in trusted
        assert EmailChannel._check_authentication_results(parsed, trusted) == (True, True)

    def test_forged_ar_matching_injected_received_is_ignored(self):
        channel = self._channel()
        parsed = self._msg(
            "mail.attacker.net; spf=pass; dkim=pass",
            received=["by user-mta.example.com; Tue, 24 Nov 2026 00:00:00 +0000"],
        )
        trusted = channel._message_trusted_authserv_ids(parsed)
        assert "mail.attacker.net" not in trusted
        assert EmailChannel._check_authentication_results(parsed, trusted) == (False, False)

    def test_no_received_headers_leaves_config_identity_only(self):
        channel = self._channel()
        parsed = self._msg("mx.example.com; spf=pass; dkim=pass")
        trusted = channel._message_trusted_authserv_ids(parsed)
        assert trusted == channel._trusted_authserv_ids


class TestEmailConfigDefaults:
    def test_list_defaults_are_instance_isolated(self):
        a = EmailConfig()
        b = EmailConfig()
        a.allow_from.append("someone@example.com")  # type: ignore[union-attr]
        assert b.allow_from == []

    def test_explicit_none_falls_back_to_empty_list(self):
        config = EmailConfig(allow_from=None)
        assert config.allow_from == []
