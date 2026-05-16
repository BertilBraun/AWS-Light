from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode


class PresignedUrlService:
    def __init__(self, secret_key: str, base_url: str) -> None:
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")

    def generate_presigned_get(self, bucket: str, object_key: str, ttl_seconds: int = 3600) -> str:
        expires_at = int(time.time()) + ttl_seconds
        params = {"bucket": bucket, "key": object_key, "expires": str(expires_at)}
        signature = self._sign(params)
        params["signature"] = signature
        return f"{self._base_url}/api/v1/storage/presigned?{urlencode(params)}"

    def validate_presigned_url(
        self, bucket: str, object_key: str, expires: str, signature: str
    ) -> bool:
        if int(expires) < int(time.time()):
            return False
        params = {"bucket": bucket, "key": object_key, "expires": expires}
        expected_signature = self._sign(params)
        return hmac.compare_digest(expected_signature, signature)

    def _sign(self, params: dict[str, str]) -> str:
        message = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        return hmac.new(
            self._secret_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
