"""Internal trigger router and production composition for Callwright escalation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from dragback.config import Settings, settings
from dragback.domain import utc_now
from dragback.notify.escalate import (
    InterruptEscalationError,
    InterruptEscalationResult,
    InterruptEscalationScanner,
)
from dragback.notify.escalation_source import (
    RepositoryInterruptEscalationSource,
    SqliteInterruptEscalationGrantStore,
)
from dragback.services.support import (
    ApiError,
    correlated_payload,
    require_internal_service,
)
from dragback.workspaces.repository import LiveWorkspaceRepository
from dragback.workspaces.session_binding import ClaudeCodeSessionRegistry
from dragback.workspaces.transport import (
    HttpLiveWorkspaceTransport,
    LiveWorkspaceTransport,
)

DEFAULT_INTERRUPT_ESCALATION_THRESHOLD_SECONDS = 300.0
DEFAULT_INTERRUPT_ESCALATION_GRANT_STORE = (
    ".dragback/interrupt-escalation-grants.json"
)
DEFAULT_INTERRUPT_ESCALATION_PHONE_REF = "demo-venue"
DEFAULT_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS = 30.0
MIN_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS = 1.0
MAX_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS = 300.0

logger = logging.getLogger(__name__)


class InterruptEscalationScanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scanned_at: datetime
    results: tuple[InterruptEscalationResult, ...] = ()


class InterruptEscalationScheduler:
    """Stoppable feature-gated loop for the deterministic scanner."""

    def __init__(
        self,
        scanner: InterruptEscalationScanner,
        *,
        enabled: bool,
        interval_seconds: float,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not (
            MIN_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS
            <= interval_seconds
            <= MAX_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS
        ):
            raise ValueError(
                "interrupt escalation scan interval must be between "
                f"{MIN_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS:g} and "
                f"{MAX_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS:g} seconds"
            )
        self._scanner = scanner
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._status_lock = RLock()
        self._scan_count = 0
        self._last_error: str | None = None
        self._last_results: tuple[InterruptEscalationResult, ...] = ()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    @property
    def scan_count(self) -> int:
        with self._status_lock:
            return self._scan_count

    @property
    def last_error(self) -> str | None:
        with self._status_lock:
            return self._last_error

    @property
    def last_results(self) -> tuple[InterruptEscalationResult, ...]:
        with self._status_lock:
            return self._last_results

    async def start(self) -> None:
        """Start one loop; disabled schedulers remain inert."""

        async with self._lifecycle_lock:
            if not self._enabled or self.running:
                return
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(
                self._run(self._stop_event),
                name="dragback-interrupt-escalation",
            )

    async def stop(self) -> None:
        """Signal the loop and wait for the current bounded scan to finish."""

        async with self._lifecycle_lock:
            task = self._task
            stop_event = self._stop_event
            if task is None:
                return
            if stop_event is not None:
                stop_event.set()
            try:
                await task
            finally:
                self._task = None
                self._stop_event = None

    def scan_once(self) -> tuple[InterruptEscalationResult, ...]:
        """Run one foreground scan, surfacing errors to the caller."""

        return self._scanner.scan(now=self._clock())

    async def _run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.to_thread(self._scan_in_background)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                continue

    def _scan_in_background(self) -> None:
        try:
            results = self.scan_once()
        except Exception as exc:
            with self._status_lock:
                self._scan_count += 1
                self._last_error = str(exc)
                self._last_results = ()
            logger.exception("Interrupt escalation background scan failed")
            return
        with self._status_lock:
            self._scan_count += 1
            self._last_error = None
            self._last_results = results


def build_interrupt_escalation_router(
    scanner: InterruptEscalationScanner,
    *,
    internal_service_secret: str,
    clock: Callable[[], datetime] = utc_now,
    scheduler: InterruptEscalationScheduler | None = None,
) -> APIRouter:
    """Expose a manual tick and bracket the periodic scanner with app lifespan."""

    @asynccontextmanager
    async def lifespan(_: object):
        if scheduler is not None:
            await scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()

    router = APIRouter(
        prefix="/internal/supervisor/escalations",
        tags=["supervisor-escalations"],
        lifespan=lifespan,
    )

    @router.post("/scan")
    def scan_interrupt_escalations(
        request: Request,
    ) -> dict[str, object]:
        require_internal_service(request, secret=internal_service_secret)
        scanned_at = clock()
        try:
            results = scanner.scan(now=scanned_at)
        except InterruptEscalationError as exc:
            raise ApiError(
                status_code=409,
                code="INTERRUPT_ESCALATION_BLOCKED",
                message=str(exc),
            ) from exc
        return correlated_payload(
            InterruptEscalationScanResponse(
                scanned_at=scanned_at,
                results=results,
            )
        )

    return router


def compose_interrupt_escalation_router(
    *,
    repository: LiveWorkspaceRepository,
    sessions: ClaudeCodeSessionRegistry,
    config: Settings = settings,
    transport: LiveWorkspaceTransport | None = None,
    threshold_seconds: float | None = None,
    scan_interval_seconds: float | None = None,
    grant_store_path: str | Path | None = None,
    phone_number_ref: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> APIRouter:
    """Construct the real repository→authority→executor escalation path."""

    selected_threshold = (
        threshold_seconds
        if threshold_seconds is not None
        else config.interrupt_escalation_threshold_seconds
    )
    threshold = timedelta(seconds=selected_threshold)
    selected_scan_interval = (
        scan_interval_seconds
        if scan_interval_seconds is not None
        else config.interrupt_escalation_scan_interval_seconds
    )
    selected_store = (
        grant_store_path
        if grant_store_path is not None
        else config.interrupt_escalation_grant_store
    )
    selected_phone_ref = (
        phone_number_ref
        if phone_number_ref is not None
        else config.interrupt_escalation_phone_ref
    )
    selected_transport = transport or HttpLiveWorkspaceTransport(config)
    source = RepositoryInterruptEscalationSource(
        repository=repository,
        sessions=sessions,
        authority=selected_transport,
        grants=SqliteInterruptEscalationGrantStore(selected_store),
        threshold=threshold,
        phone_number_ref=selected_phone_ref,
    )
    scanner = InterruptEscalationScanner(
        source=source,
        acknowledgements=sessions,
        executor=selected_transport,
        threshold=threshold,
        callwright_live_calls_enabled=config.callwright_live_calls_enabled,
    )
    scheduler = InterruptEscalationScheduler(
        scanner,
        enabled=config.callwright_live_calls_enabled,
        interval_seconds=selected_scan_interval,
        clock=clock,
    )
    return build_interrupt_escalation_router(
        scanner,
        internal_service_secret=config.grant_secret,
        clock=clock,
        scheduler=scheduler,
    )
