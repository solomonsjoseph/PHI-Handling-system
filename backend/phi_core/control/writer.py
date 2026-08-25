"""``ArtifactWriter`` (D14): the sole task-facing artifact staging facade.

Wraps :class:`~.artifacts.ArtifactService` with the two-phase stage/finalize
contract the :class:`~.context.ArtifactWriter` protocol agents receive
requires, binding a fixed ``producer_task_id`` so callers never have to
thread it through every call.

The atomicity guarantee belongs entirely to ``ArtifactService``: nothing
here writes a byte itself, and nothing here has to defend against a
producer raising between ``stage`` and ``finalize`` -- ``finalize`` is the
only thing that ever promotes bytes to the real (non-``.tmp``) path, so a
caller that never reaches it (because the producer raised) leaves the
artifact record ``provisional`` and no bytes anywhere but the untouched
``.tmp`` staging file. The ``write`` context manager below makes that the
default: a raise inside the ``async with`` block propagates unchanged and
``finalize`` is simply never called.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .artifacts import ArtifactService
from .records import ArtifactRecord, DataClass


class ArtifactWriter:
    """Task-scoped staging facade bound to one ``ArtifactService`` and task."""

    def __init__(self, service: ArtifactService, *, producer_task_id: str = "") -> None:
        self._service = service
        self._producer_task_id = producer_task_id

    @property
    def session_id(self) -> str:
        return self._service.session_id

    @property
    def run_id(self) -> str:
        return self._service.run_id

    async def stage(
        self,
        type: str,
        filename: str,
        data_class: DataClass,
        retention_class: str,
        *,
        scope: str = "run",
        root: str = "staging",
    ) -> tuple[str, Any]:
        """Register a ``provisional`` artifact and return ``(artifact_id, tmp_path)``.

        The caller writes the complete object to ``tmp_path`` and then
        calls :meth:`finalize`. Prefer :meth:`write` when the producer is a
        single call that either completes or raises -- it makes the
        stage-then-finalize-only-on-success discipline structural instead
        of something every call site has to get right by hand.
        """
        return await self._service.stage(
            type, filename, data_class, retention_class,
            producer_task_id=self._producer_task_id, scope=scope, root=root,
        )

    async def finalize(self, artifact_id: str) -> ArtifactRecord:
        """Hash, atomically promote, and mark ``artifact_id`` ``staged``.

        Never call this unless the producer finished writing ``tmp_path``
        without raising. That is the entire two-phase contract: an
        exception between ``stage`` and this call leaves the artifact
        ``provisional`` and no bytes at the real path, and this method has
        nothing to clean up because it never ran.
        """
        return await self._service.finalize(artifact_id)

    @asynccontextmanager
    async def write(
        self,
        type: str,
        filename: str,
        data_class: DataClass,
        retention_class: str,
        *,
        scope: str = "run",
        root: str = "staging",
    ) -> AsyncIterator[tuple[str, Any]]:
        """Stage, yield ``(artifact_id, tmp_path)`` for the body to write to,
        and finalize only if the body returns without raising.

        A raise inside the ``async with`` block propagates unchanged;
        ``finalize`` is simply never reached, so the artifact stays
        ``provisional`` and no promotable partial file exists under the
        real (non-``.tmp``) root -- the exact guarantee
        ``test_control_writer.py`` proves directly.
        """
        artifact_id, tmp_path = await self.stage(
            type, filename, data_class, retention_class, scope=scope, root=root,
        )
        yield artifact_id, tmp_path
        await self.finalize(artifact_id)
