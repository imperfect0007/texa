import hashlib
import hmac
from typing import Mapping, Optional


def verify_webhook_get(
    query_params: Mapping[str, str],
    expected_verify_token: str,
) -> bool:
    """
    Verify Meta's webhook challenge handshake.

    Meta sends:
      hub.mode=subscribe
      hub.verify_token=<your verify token>
      hub.challenge=<challenge id>
    """
    mode = query_params.get("hub.mode", "")
    verify_token = query_params.get("hub.verify_token", "")

    return mode == "subscribe" and verify_token == expected_verify_token


def verify_webhook_signature(
    request_body: bytes,
    signature_header: Optional[str],
    app_secret: Optional[str],
) -> bool:
    """
    Verify Meta webhook request signature (X-Hub-Signature-256).

    If app_secret is not provided, signature verification is skipped.
    """
    if not app_secret:
        return True

    if not signature_header:
        return False

    # Header format: "sha256=<hex_digest>"
    try:
        received = signature_header.split("=", 1)[1].strip()
    except IndexError:
        return False

    digest = hmac.new(
        app_secret.encode("utf-8"),
        msg=request_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(received, digest)

