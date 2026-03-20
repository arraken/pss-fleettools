from pssapi.utils.exceptions import PssApiError


def is_pssapi_rate_limit_error(error: Exception) -> bool:
    """Returns True if the error is a PSS API rate limit (HTTP 429) error."""
    if isinstance(error, PssApiError):
        message = str(error).lower()
        return "rate limit" in message or "too many requests" in message or "429" in message
    return False


def is_pssapi_token_error(error: Exception) -> bool:
    """Returns True if the error indicates an invalid or expired access token."""
    if isinstance(error, PssApiError):
        message = str(error).lower()
        return (
            "token" in message
            or "unauthorized" in message
            or "401" in message
            or "forbidden" in message
            or "access" in message
        )
    return False

