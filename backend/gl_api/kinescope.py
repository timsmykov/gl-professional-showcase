"""Read-only Kinescope adapter. Never return provider secrets or media download URLs."""

import asyncio
import json
import re
from pathlib import Path
from uuid import UUID

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gl_api.config import ConfigurationError, read_secret

API_ORIGIN = "https://api.kinescope.io"
MAX_RESPONSE_BYTES = 1024 * 1024


class KinescopeError(RuntimeError):
    """Only fixed messages may cross the operational boundary."""


def load_api_token(token_file: Path) -> str:
    token = read_secret(token_file, "Kinescope API token file")
    try:
        UUID(token)
    except ValueError:
        raise ConfigurationError("Kinescope API token must be a UUID") from None
    return token


def resource_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", value):
        raise ValueError("invalid Kinescope resource ID")
    return value


class Video(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    id: str
    project_id: str
    status: str
    duration: float = Field(ge=0, allow_inf_nan=False)


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    id: str
    encrypted: bool | None = None


class DrmPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    url: str
    # Some documented GET responses omit strict. Missing is NOT proof of protection.
    strict: bool | None = None

    def verified_for(self, expected_url: str) -> bool:
        return self.strict is True and self.url == expected_url and self.url.startswith("https://")


class KinescopeClient:
    def __init__(self, session: aiohttp.ClientSession, token_file: Path):
        self._token = load_api_token(token_file)
        self._session = session

    async def _get(self, path: str, params: dict | None = None):
        try:
            async with self._session.get(
                API_ORIGIN + path,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                allow_redirects=False,
                ssl=True,
                timeout=aiohttp.ClientTimeout(total=8, connect=3),
            ) as response:
                if response.status != 200:
                    reason = {
                        401: "Kinescope authentication failed",
                        403: "Kinescope permission denied",
                        404: "Kinescope resource unavailable",
                        429: "Kinescope rate limited",
                    }.get(response.status, "Kinescope request failed")
                    raise KinescopeError(reason)
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise KinescopeError("Kinescope response too large")
                payload = json.loads(body)
                if not isinstance(payload, dict) or "data" not in payload:
                    raise KinescopeError("Kinescope response invalid")
                return payload["data"]
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise KinescopeError("Kinescope unavailable") from None
        except (ValueError, RecursionError):
            raise KinescopeError("Kinescope response invalid") from None

    async def video(self, video_id: str) -> Video:
        data = await self._get(f"/v1/videos/{resource_id(video_id)}")
        try:
            result = Video.model_validate(data)
            if result.id != video_id:
                raise ValueError("identity mismatch")
            resource_id(result.project_id)
            return result
        except (ValueError, ValidationError):
            raise KinescopeError("Kinescope response invalid") from None

    async def projects(self, page: int = 1) -> list[Project]:
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError("invalid project page")
        data = await self._get("/v1/projects", {"page": page, "per_page": 100})
        try:
            if not isinstance(data, list) or len(data) > 100:
                raise ValueError("invalid project list")
            result = [Project.model_validate(item) for item in data]
            for item in result:
                resource_id(item.id)
            return result
        except ValueError:
            raise KinescopeError("Kinescope response invalid") from None

    async def drm_policy(self, project_id: str) -> DrmPolicy:
        data = await self._get(f"/v1/drm/auth/{resource_id(project_id)}")
        try:
            return DrmPolicy.model_validate(data)
        except ValidationError:
            raise KinescopeError("Kinescope response invalid") from None


async def inspect(token_file: Path, project_id: str) -> dict:
    """Private operator check. Output only safe counts/flags, never raw provider JSON."""
    async with aiohttp.ClientSession(trust_env=False) as session:
        client = KinescopeClient(session, token_file)
        policy = await client.drm_policy(project_id)
        return {"api_authenticated": True, "strict_confirmed": policy.strict is True}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read-only Kinescope project DRM inspection")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(inspect(args.token_file, args.project_id))))
    except (ConfigurationError, KinescopeError, ValueError):
        print('{"status":"inspection_failed"}')
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
