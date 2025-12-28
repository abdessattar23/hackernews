from typing import Optional

import boto3


class S3Store:
    def __init__(self, bucket: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._s3 = boto3.client("s3")

    def _key(self, rel: str) -> str:
        rel = rel.lstrip("/")
        if not self._prefix:
            return rel
        return f"{self._prefix}/{rel}"

    def put_text(self, rel_key: str, content: str, content_type: str) -> str:
        key = self._key(rel_key)
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )
        return key

    def put_bytes(self, rel_key: str, content: bytes, content_type: str) -> str:
        key = self._key(rel_key)
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )
        return key

    def public_url(self, key: str, region: Optional[str] = None) -> str:
        if region:
            return f"https://{self._bucket}.s3.{region}.amazonaws.com/{key}"
        return f"https://{self._bucket}.s3.amazonaws.com/{key}"
