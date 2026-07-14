#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

SENSITIVE_RESPONSE_KEYS = {
    'Auth',
    'Token',
    'SID',
    'LSID',
    'EncryptedPasswd',
    'oauth_token',
    'Url',
}


class GoogleAuthError(RuntimeError):
    pass


def require_response_field(response, field, context):
    if not isinstance(response, dict):
        raise GoogleAuthError(
            f"{context} failed: unexpected response type {type(response).__name__}."
        )

    value = response.get(field)
    if value:
        return value

    raise GoogleAuthError(
        f"{context} failed: Google auth response did not include '{field}'. "
        f"{_safe_response_summary(response)}"
    )


def _safe_response_summary(response):
    safe_items = []

    for key, value in response.items():
        if key in SENSITIVE_RESPONSE_KEYS:
            continue

        text = str(value)
        if len(text) > 300:
            text = text[:297] + '...'
        safe_items.append(f"{key}={text}")

    if safe_items:
        return "Returned fields: " + ", ".join(safe_items)

    keys = [key for key in response.keys() if key not in SENSITIVE_RESPONSE_KEYS]
    if keys:
        return "Returned non-sensitive keys: " + ", ".join(keys)

    return (
        "No non-sensitive error fields were returned. The cached AAS token may be "
        "expired; re-run authentication or refresh Auth/secrets.json."
    )
