from __future__ import annotations

from statewave import AsyncStatewaveClient as _SDKAsyncStatewaveClient
from statewave import StatewaveError


class AsyncStatewaveClient:
    """Thin app-level wrapper around the official Statewave SDK client.

    Keeps the dict-based interface this app's callers (analyst.py, server.py)
    already use, while delegating all HTTP/retry/polling to the SDK.
    """

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self._sdk = _SDKAsyncStatewaveClient(base_url, api_key=api_key)

    async def post_episode(
        self,
        subject_id: str,
        source: str,
        type: str,
        payload: dict,
        metadata: dict | None = None,
    ) -> dict:
        episode = await self._sdk.create_episode(
            subject_id, source, type, payload, metadata=metadata
        )
        return episode.model_dump(mode="json")

    async def compile_memories(self, subject_id: str) -> dict:
        job = await self._sdk.compile_memories_wait(subject_id)
        return job.model_dump(mode="json")

    async def get_context(self, subject_id: str, task: str, max_tokens: int = 3000) -> dict:
        ctx = await self._sdk.get_context(subject_id, task, max_tokens=max_tokens)
        return ctx.model_dump(mode="json")

    async def get_timeline(self, subject_id: str) -> dict:
        try:
            timeline = await self._sdk.get_timeline(subject_id)
        except StatewaveError as e:
            if getattr(e, "status_code", None) == 404:
                return {}
            raise
        return timeline.model_dump(mode="json")

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
        result = await self._sdk.delete_subject(subject_id)
        return result.model_dump(mode="json")

    async def aclose(self) -> None:
        await self._sdk.close()

    async def __aenter__(self) -> "AsyncStatewaveClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
