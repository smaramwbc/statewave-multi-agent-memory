from __future__ import annotations

import asyncio

import httpx


class StatewaveError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AsyncStatewaveClient:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            h["X-API-Key"] = self._api_key
        return h

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            body = resp.json()
            msg = body.get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text
        raise StatewaveError(f"Statewave {resp.status_code}: {msg}", resp.status_code)

    async def post_episode(
        self,
        subject_id: str,
        source: str,
        type: str,
        payload: dict,
        metadata: dict | None = None,
    ) -> dict:
        body: dict = {"subject_id": subject_id, "source": source, "type": type, "payload": payload}
        if metadata:
            body["metadata"] = metadata
        resp = await self._client.post(f"{self._base_url}/v1/episodes", json=body, headers=self._headers())
        self._raise_for_status(resp)
        return resp.json()

    async def compile_memories(self, subject_id: str, _poll_attempts: int = 10, _poll_interval: float = 0.3) -> dict:
        resp = await self._client.post(
            f"{self._base_url}/v1/memories/compile",
            json={"subject_id": subject_id},
            headers=self._headers(),
        )
        self._raise_for_status(resp)
        result = resp.json() if resp.status_code != 202 else {}
        # 202 Accepted means compile is async server-side; poll timeline until
        # memory count stabilises before the caller diffs the result.
        if resp.status_code == 202:
            prev_count = -1
            for _ in range(_poll_attempts):
                await asyncio.sleep(_poll_interval)
                tl = await self.get_timeline(subject_id)
                count = len(tl.get("memories", []))
                if count == prev_count:
                    break
                prev_count = count
        return result

    async def get_context(self, subject_id: str, task: str, max_tokens: int = 3000) -> dict:
        resp = await self._client.post(
            f"{self._base_url}/v1/context",
            json={"subject_id": subject_id, "task": task, "max_tokens": max_tokens},
            headers=self._headers(),
        )
        self._raise_for_status(resp)
        return resp.json()

    async def get_timeline(self, subject_id: str) -> dict:
        resp = await self._client.get(
            f"{self._base_url}/v1/timeline",
            params={"subject_id": subject_id},
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return {}
        self._raise_for_status(resp)
        return resp.json()

    async def search_memories(self, subject_id: str) -> list[dict]:
        """Return all memories for a subject via the timeline endpoint."""
        timeline = await self.get_timeline(subject_id)
        return timeline.get("memories", [])

    async def get_memory_diff(self, subject_id: str, before_ids: set[str]) -> dict[str, list[dict]]:
        timeline = await self.get_timeline(subject_id)
        all_memories = timeline.get("memories", [])

        new: list[dict] = []
        superseded: list[dict] = []
        unchanged: list[dict] = []

        for mem in all_memories:
            mem_id = mem.get("id", "")
            status = mem.get("status", "active")
            if status == "active":
                if mem_id not in before_ids:
                    new.append(mem)
                else:
                    unchanged.append(mem)
            elif status == "superseded" and mem_id in before_ids:
                superseded.append(mem)

        return {"new": new, "superseded": superseded, "unchanged": unchanged}

    async def delete_subject(self, subject_id: str) -> dict:
        resp = await self._client.delete(
            f"{self._base_url}/v1/subjects/{subject_id}",
            headers=self._headers(),
        )
        self._raise_for_status(resp)
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncStatewaveClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
