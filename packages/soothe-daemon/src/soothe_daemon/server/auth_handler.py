"""WebSocket auth message handler.

Processes ``auth`` (AKSK → tokens) and ``auth_refresh`` (refresh → rotated
tokens) messages. Messages are processed before IdentityMiddleware validation.
"""

from __future__ import annotations

import logging

from soothe_sdk.protocols.identity import IdentityProtocol

logger = logging.getLogger(__name__)

_AUTH_ERROR_MESSAGES: dict[str, str] = {
    "invalid_credentials": "Access key or secret key is invalid",
    "aksk_expired": "AKSK has expired",
    "aksk_revoked": "AKSK has been revoked",
    "missing_credentials": "Access key and secret key are required",
    "identity_disabled": "Identity service is not enabled on this daemon",
}

_REFRESH_ERROR_MESSAGES: dict[str, str] = {
    "invalid_refresh_token": "Refresh token is invalid, expired, or revoked",
    "missing_refresh_token": "Refresh token is required",
    "identity_disabled": "Identity service is not enabled on this daemon",
}


class AuthHandler:
    """Handle WebSocket auth/auth_refresh messages.

    Wraps an `IdentityProtocol` implementation and translates authentication
    results into wire response dicts.

    Example:
        handler = AuthHandler(identity_service)
        response = handler.handle_auth(access_key, secret_key)
    """

    def __init__(self, identity: IdentityProtocol) -> None:
        """Initialize AuthHandler.

        Args:
            identity: IdentityProtocol implementation for authentication.
        """
        self._identity = identity

    def handle_auth(self, access_key: str, secret_key: str) -> dict:
        """Process auth message with AKSK credentials.

        Args:
            access_key: Access key (AK-{16 chars}).
            secret_key: Secret key (SK-{32 chars}).

        Returns:
            auth_response message dict with success/error.
        """
        logger.debug("Processing auth request: access_key=%s", access_key[:6] + "...")

        result = self._identity.authenticate(access_key, secret_key)

        if result is None:
            logger.warning("Authentication failed: invalid credentials")
            return build_auth_response_error("invalid_credentials")

        logger.info(
            "Authentication successful: user=%s expires_in=%ds",
            result.user_id,
            result.expires_in,
        )

        return {
            "type": "auth_response",
            "success": True,
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "expires_in": result.expires_in,
            "user_id": result.user_id,
        }

    def handle_refresh(self, refresh_token: str) -> dict:
        """Process auth_refresh message.

        Args:
            refresh_token: JWT refresh token.

        Returns:
            auth_refresh_response message dict with success/error.
        """
        logger.debug("Processing token refresh request")

        result = self._identity.refresh_token(refresh_token)

        if result is None:
            logger.warning("Token refresh failed: invalid or expired refresh token")
            return build_refresh_response_error("invalid_refresh_token")

        logger.info("Token refresh successful: expires_in=%ds", result.expires_in)

        return {
            "type": "auth_refresh_response",
            "success": True,
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "expires_in": result.expires_in,
        }


def _build_error_response(
    *,
    message_type: str,
    error_messages: dict[str, str],
    error_code: str,
    message: str | None = None,
    default_message: str = "Authentication failed",
) -> dict:
    """Build a standardized auth error response.

    Args:
        message_type: Wire message type (e.g. "auth_response").
        error_messages: Mapping of error codes to default messages.
        error_code: Error code (e.g. invalid_credentials, invalid_refresh_token).
        message: Optional custom message (generic messages preferred for security).
        default_message: Fallback message when error_code is not in error_messages.

    Returns:
        Wire response dict with success=False.
    """
    return {
        "type": message_type,
        "success": False,
        "error": error_code,
        "message": message or error_messages.get(error_code, default_message),
    }


def build_auth_response_error(error_code: str, message: str | None = None) -> dict:
    """Build a standardized auth error response.

    Args:
        error_code: Error code (invalid_credentials, aksk_expired, etc.).
        message: Optional custom message (generic messages preferred for security).

    Returns:
        auth_response dict with success=False.
    """
    return _build_error_response(
        message_type="auth_response",
        error_messages=_AUTH_ERROR_MESSAGES,
        error_code=error_code,
        message=message,
    )


def build_refresh_response_error(error_code: str, message: str | None = None) -> dict:
    """Build a standardized refresh error response.

    Args:
        error_code: Error code (invalid_refresh_token, etc.).
        message: Optional custom message.

    Returns:
        auth_refresh_response dict with success=False.
    """
    return _build_error_response(
        message_type="auth_refresh_response",
        error_messages=_REFRESH_ERROR_MESSAGES,
        error_code=error_code,
        message=message,
        default_message="Token refresh failed",
    )
