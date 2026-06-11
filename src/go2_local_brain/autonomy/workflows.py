"""Declarative, interruptible workflows so the AI follows a routine.

Instead of prompting every action, the operator activates a named
*workflow* — a list of high-level steps the engine runs until it finishes
or is stopped. Workflows are data (``Workflow(name, steps=[Step(...), ...])``)
interpreted by ``WorkflowEngine``; adding a behavior is adding a step
handler, not rewriting control flow.

Everything here is hardware-agnostic: steps call into a duck-typed
``WorkflowContext`` whose ``robot`` exposes the same async methods the
driver already has (``move``/``stop``/``stand_up``/``sit_down``/
``advanced_action``/``explore_room``). Tests drive it with a fake robot,
a fake observation source, and a fake targeting controller — no robot, no
ML, no asyncio hangs (every step runs under a timeout).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

from .perception import Observation

log = logging.getLogger(__name__)

# Cap on any single step so a wedged move/observe can't freeze the whole
# workflow loop (same lesson as the autonomy supervisor).
_STEP_TIMEOUT_S = 20.0


# ----------------------------------------------------------------- data model


@dataclass(frozen=True)
class Step:
    """One workflow instruction: a kind + its parameters."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Workflow:
    """A named, ordered list of steps."""

    name: str
    description: str
    steps: list[Step] = field(default_factory=list)


@dataclass
class WorkflowContext:
    """Capabilities a running workflow can use.

    ``robot`` must provide the async driver surface (move/stop/stand_up/
    sit_down/advanced_action; explore_room optional). The rest are optional
    callables so the engine degrades gracefully when a capability is absent.
    """

    robot: Any
    observe: Optional[Callable[[], Awaitable[Optional[Observation]]]] = None
    face_tracks: Optional[Callable[[], Sequence[Any]]] = None
    targeting: Any = None  # autonomy.targeting.TargetingController
    event_sink: Optional[Callable[[str], None]] = None

    def event(self, message: str) -> None:
        log.info("workflow: %s", message)
        if self.event_sink is not None:
            self.event_sink(message)


@dataclass
class WorkflowStatus:
    state: str  # idle | running | done | error | stopped
    workflow: Optional[str]
    step_index: int
    step_kind: Optional[str]
    last_event: str
    events: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- engine


class WorkflowEngine:
    """Runs one workflow at a time, interruptibly."""

    def __init__(self, context: WorkflowContext, *, max_events: int = 100) -> None:
        self._ctx = context
        self._registry: dict[str, Workflow] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._state = "idle"
        self._active: Optional[str] = None
        self._step_index = 0
        self._step_kind: Optional[str] = None
        self._events: list[str] = []
        self._max_events = max_events
        for wf in builtin_workflows():
            self.register(wf)

    # ---------------------------------------------------------- registry

    def register(self, workflow: Workflow) -> None:
        self._registry[workflow.name] = workflow

    def list_workflows(self) -> list[dict[str, str]]:
        return [
            {"name": wf.name, "description": wf.description, "steps": str(len(wf.steps))}
            for wf in self._registry.values()
        ]

    def has(self, name: str) -> bool:
        return name in self._registry

    # ---------------------------------------------------------- lifecycle

    async def start(self, name: str) -> bool:
        """Start a workflow by name. Returns False if unknown or already running."""
        if name not in self._registry:
            self._record(f"unknown workflow {name!r}")
            return False
        if self._task is not None and not self._task.done():
            self._record("a workflow is already running; stop it first")
            return False
        self._stop.clear()
        self._active = name
        self._state = "running"
        self._step_index = 0
        self._task = asyncio.create_task(self._run(self._registry[name]), name=f"workflow-{name}")
        self._record(f"started {name}")
        return True

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Best-effort halt.
        try:
            await self._ctx.robot.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._state == "running":
            self._state = "stopped"
        self._record("stopped")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> WorkflowStatus:
        return WorkflowStatus(
            state=self._state,
            workflow=self._active,
            step_index=self._step_index,
            step_kind=self._step_kind,
            last_event=self._events[-1] if self._events else "",
            events=list(self._events),
        )

    # ---------------------------------------------------------- internals

    async def _run(self, workflow: Workflow) -> None:
        try:
            await self._run_steps(workflow.steps)
            if not self._stop.is_set():
                self._state = "done"
                self._record(f"{workflow.name} complete")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._state = "error"
            self._record(f"error: {exc}")
            try:
                await self._ctx.robot.stop()
            except Exception:  # noqa: BLE001
                pass

    async def _run_steps(self, steps: Sequence[Step]) -> None:
        for index, step in enumerate(steps):
            if self._stop.is_set():
                return
            self._step_index = index
            self._step_kind = step.kind
            handler = _STEP_HANDLERS.get(step.kind)
            if handler is None:
                self._record(f"unknown step kind {step.kind!r}; skipping")
                continue
            try:
                # loop handler manages its own (possibly long) lifetime; don't
                # cap it. Everything else runs under the per-step timeout.
                if step.kind == "loop":
                    await handler(self, step.params)
                else:
                    await asyncio.wait_for(handler(self, step.params), timeout=_STEP_TIMEOUT_S)
            except asyncio.TimeoutError:
                self._record(f"step {step.kind} timed out")
                await self._safe_stop()

    async def _safe_stop(self) -> None:
        try:
            await self._ctx.robot.stop()
        except Exception:  # noqa: BLE001
            pass

    def _record(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._events.append(f"{stamp} {message}")
        if len(self._events) > self._max_events:
            del self._events[: len(self._events) - self._max_events]
        self._ctx.event(message)

    # exposed for step handlers
    @property
    def ctx(self) -> WorkflowContext:
        return self._ctx

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()


# ----------------------------------------------------------------- step handlers
# Each handler is ``async (engine, params) -> None``.


async def _step_say(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    engine._record(str(params.get("text", "")))


async def _step_wait(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    seconds = float(params.get("seconds", 1.0))
    # Sleep in small slices so stop() is responsive.
    end = time.monotonic() + seconds
    while time.monotonic() < end and not engine.stop_requested:
        await asyncio.sleep(min(0.1, max(0.0, end - time.monotonic())))


async def _step_stand(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    await engine.ctx.robot.stand_up()


async def _step_sit(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    await engine.ctx.robot.sit_down()


async def _step_stop(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    await engine.ctx.robot.stop()


async def _step_move(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    await engine.ctx.robot.move(
        float(params.get("vx", 0.0)),
        float(params.get("vy", 0.0)),
        float(params.get("vyaw", 0.0)),
        float(params.get("duration_s", 0.4)),
    )


async def _step_explore(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    robot = engine.ctx.robot
    explore = getattr(robot, "explore_room", None)
    if explore is None:
        engine._record("explore unavailable; substituting a short scan")
        await _step_scan(engine, {"turns": 2})
        return
    await explore(float(params.get("duration_s", 8.0)), mode=params.get("mode"))


async def _step_scan(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    """Rotate in place a few times to look around."""
    turns = int(params.get("turns", 3))
    vyaw = float(params.get("vyaw", 0.6))
    dur = float(params.get("turn_duration_s", 0.5))
    for _ in range(max(1, turns)):
        if engine.stop_requested:
            return
        await engine.ctx.robot.move(0.0, 0.0, vyaw, dur)
        await asyncio.sleep(0.1)


async def _step_greet(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    action = getattr(engine.ctx.robot, "advanced_action", None)
    if action is None:
        engine._record("greet unavailable on this robot")
        return
    try:
        await action("greet")
    except Exception as exc:  # noqa: BLE001
        engine._record(f"greet failed: {exc}")


async def _step_greet_if_known(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    """Greet only if a known (enrolled) face is currently tracked."""
    known = _known_labels(engine)
    if known:
        engine._record(f"greeting known face(s): {', '.join(known)}")
        await _step_greet(engine, {})
    else:
        engine._record("no known face; not greeting")


async def _step_scan_for_person(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    """Rotate until a person is detected or max_turns is exhausted."""
    max_turns = int(params.get("max_turns", 8))
    vyaw = float(params.get("vyaw", 0.6))
    dur = float(params.get("turn_duration_s", 0.45))
    for _ in range(max(1, max_turns)):
        if engine.stop_requested:
            return
        obs = await _observe(engine)
        if obs is not None and any(d.is_human() for d in obs.detections):
            engine._record("person found")
            await engine.ctx.robot.stop()
            return
        await engine.ctx.robot.move(0.0, 0.0, vyaw, dur)
        await asyncio.sleep(0.1)
    engine._record("scan finished; no person found")


async def _step_targeting(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    """Run the phone-user targeting controller over a bounded window.

    This is the core of the "track phones, fire Nerf" mode. Aiming is
    always safe; firing only happens if the TargetingController is armed
    and all of its safety gates pass.
    """
    controller = engine.ctx.targeting
    if controller is None:
        engine._record("targeting unavailable (no controller wired)")
        return
    duration = float(params.get("duration_s", 5.0))
    tick = float(params.get("tick_s", 0.25))
    end = time.monotonic() + duration
    while time.monotonic() < end and not engine.stop_requested:
        obs = await _observe(engine)
        if obs is None:
            await asyncio.sleep(tick)
            continue
        decision = await controller.step(obs)
        if decision.has_target and abs(decision.aim_vyaw) > 1e-3:
            # Turn to center the target.
            await engine.ctx.robot.move(0.0, 0.0, decision.aim_vyaw, tick)
        if decision.fired:
            engine._record(f"FIRED at {decision.target_label}")
        await asyncio.sleep(tick)


async def _step_loop(engine: WorkflowEngine, params: dict[str, Any]) -> None:
    """Repeat a sub-list of steps. count<=0 loops until stop()."""
    count = int(params.get("count", 0))
    raw_steps = params.get("steps", [])
    steps = [_coerce_step(s) for s in raw_steps]
    iterations = 0
    while not engine.stop_requested:
        # Guarantee a yield to the event loop every iteration. Without this,
        # a loop whose inner steps all complete synchronously (e.g. fast
        # conditionals, or instant moves) would peg a CPU core and starve
        # the WebRTC keepalive / the engine's own stop(). asyncio.sleep(0)
        # is the canonical "let other tasks run" yield.
        await asyncio.sleep(0)
        await engine._run_steps(steps)
        iterations += 1
        if count > 0 and iterations >= count:
            return


_STEP_HANDLERS: dict[str, Callable[[WorkflowEngine, dict[str, Any]], Awaitable[None]]] = {
    "say": _step_say,
    "wait": _step_wait,
    "stand": _step_stand,
    "sit": _step_sit,
    "stop": _step_stop,
    "move": _step_move,
    "explore": _step_explore,
    "scan": _step_scan,
    "greet": _step_greet,
    "greet_if_known": _step_greet_if_known,
    "scan_for_person": _step_scan_for_person,
    "targeting": _step_targeting,
    "loop": _step_loop,
}


# ----------------------------------------------------------------- helpers


async def _observe(engine: WorkflowEngine) -> Optional[Observation]:
    if engine.ctx.observe is None:
        return None
    try:
        return await engine.ctx.observe()
    except Exception as exc:  # noqa: BLE001
        engine._record(f"observe failed: {exc}")
        return None


def _known_labels(engine: WorkflowEngine) -> list[str]:
    if engine.ctx.face_tracks is None:
        return []
    try:
        tracks = engine.ctx.face_tracks()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for t in tracks:
        if getattr(t, "is_known", False):
            label = getattr(t, "label", None)
            if label:
                out.append(str(label))
    return sorted(set(out))


def _coerce_step(raw: Any) -> Step:
    if isinstance(raw, Step):
        return raw
    if isinstance(raw, dict):
        return Step(kind=str(raw.get("kind", "")), params=dict(raw.get("params", {})))
    raise ValueError(f"cannot coerce {raw!r} to Step")


# ----------------------------------------------------------------- built-ins


def builtin_workflows() -> list[Workflow]:
    """The library of ready-to-run workflows."""
    return [
        Workflow(
            name="patrol_and_greet",
            description="Roam the room; greet anyone the robot recognizes.",
            steps=[
                Step("stand"),
                Step("loop", {"count": 0, "steps": [
                    {"kind": "explore", "params": {"duration_s": 8.0, "mode": "telemetry"}},
                    {"kind": "greet_if_known"},
                    {"kind": "wait", "params": {"seconds": 1.0}},
                ]}),
            ],
        ),
        Workflow(
            name="find_person",
            description="Rotate to find a person, then stop and watch.",
            steps=[
                Step("stand"),
                Step("scan_for_person", {"max_turns": 8}),
                Step("say", {"text": "watching"}),
            ],
        ),
        Workflow(
            name="guard_post",
            description="Stand guard; periodically greet known faces.",
            steps=[
                Step("stand"),
                Step("loop", {"count": 0, "steps": [
                    {"kind": "wait", "params": {"seconds": 3.0}},
                    {"kind": "greet_if_known"},
                ]}),
            ],
        ),
        Workflow(
            name="phone_tracker",
            description="Track people using phones and (if armed) fire the Nerf launcher.",
            steps=[
                Step("stand"),
                Step("loop", {"count": 0, "steps": [
                    {"kind": "targeting", "params": {"duration_s": 4.0, "tick_s": 0.25}},
                    {"kind": "wait", "params": {"seconds": 0.5}},
                ]}),
            ],
        ),
    ]
