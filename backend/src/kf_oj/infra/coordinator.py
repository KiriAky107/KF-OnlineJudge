"""Bounded loopback control adapter; never owns evaluation database writes."""

from uuid import UUID

import httpx


class CoordinatorControl:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    async def cancel(self, evaluation_id: UUID) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=3, trust_env=False, follow_redirects=False
            ) as client:
                # Response has no business payload. Do not buffer or log arbitrary remote bodies.
                async with client.stream(
                    "POST",
                    self.url + "/internal/cancel",
                    headers={"Authorization": "Bearer " + self.token},
                    json={
                        "schema_version": 1,
                        "kind": "judge.evaluation",
                        "evaluation_id": str(evaluation_id),
                    },
                ) as response:
                    return response.status_code == 204
        except httpx.HTTPError:
            return False
