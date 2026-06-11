"""Phone-user targeting + Nerf control.

Scope
-----
This drives the "track people on their phone and fire the (foam) Nerf gun"
mode. It is built **safe-by-default**:

* The default ``NerfController`` is ``LoggingNerfController`` — it logs
  "would fire" and never actuates anything. Nothing physically fires until
  you explicitly construct a ``SerialNerfController`` AND arm it.
* Firing is gated behind multiple conditions that must ALL hold:
  - the controller is armed (disarmed at construction),
  - a valid phone-using person target is locked,
  - the target has stayed centered for N consecutive frames,
  - a per-shot cooldown has elapsed,
  - the per-session fire cap has not been reached.
* Aiming (turning the robot to center a target) is independent of firing
  and is always safe to run.

The Arduino-driven launcher is out of scope here (wired later). The
``SerialNerfController`` just writes a trigger byte to a serial port; the
firmware on the Arduino does the rest.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .perception import Detection, Observation

log = logging.getLogger(__name__)


# ----------------------------------------------------------------- Nerf control


class NerfController(Protocol):
    """Minimal launcher interface. Implementations must be safe when disarmed."""

    @property
    def armed(self) -> bool: ...

    def arm(self) -> None: ...

    def disarm(self) -> None: ...

    async def fire(self) -> bool:
        """Attempt one shot. Returns True if a shot was actually issued."""
        ...

    def status(self) -> dict[str, Any]: ...


class _BaseNerf:
    """Shared arm/disarm + counters for concrete controllers."""

    def __init__(self) -> None:
        self._armed = False
        self._shots = 0

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self) -> None:
        self._armed = True
        log.warning("nerf ARMED")

    def disarm(self) -> None:
        self._armed = False
        log.info("nerf disarmed")

    def status(self) -> dict[str, Any]:
        return {"armed": self._armed, "shots": self._shots, "backend": type(self).__name__}


class LoggingNerfController(_BaseNerf):
    """Default: logs intent, never actuates hardware. Safe for testing."""

    async def fire(self) -> bool:
        if not self._armed:
            log.info("nerf fire requested but disarmed; ignoring")
            return False
        self._shots += 1
        log.warning("nerf WOULD FIRE (logging backend, shot #%d)", self._shots)
        return True


class SerialNerfController(_BaseNerf):
    """Writes a trigger byte to an Arduino over serial. Lazy pyserial import.

    The serial port is opened on first ``fire()`` so importing this module
    never requires pyserial or a connected device.
    """

    def __init__(self, port: str = "/dev/ttyACM0", baud: int = 115200, trigger_byte: bytes = b"F") -> None:
        super().__init__()
        self._port = port
        self._baud = baud
        self._trigger = trigger_byte
        self._serial: Any = None

    def _ensure(self) -> None:
        if self._serial is not None:
            return
        try:
            import serial  # type: ignore
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError("pyserial required for SerialNerfController; pip install pyserial") from exc
        self._serial = serial.Serial(self._port, self._baud, timeout=0.2)

    async def fire(self) -> bool:
        if not self._armed:
            log.info("nerf fire requested but disarmed; ignoring")
            return False
        try:
            self._ensure()
            self._serial.write(self._trigger)
            self._serial.flush()
        except Exception as exc:  # noqa: BLE001
            log.error("nerf serial fire failed: %s", exc)
            return False
        self._shots += 1
        log.warning("nerf FIRED via serial (shot #%d)", self._shots)
        return True

    def status(self) -> dict[str, Any]:
        s = super().status()
        s["port"] = self._port
        return s


def build_nerf_controller(backend: str = "logging", **kwargs: Any) -> NerfController:
    """Construct a launcher. Default 'logging' never actuates hardware."""
    key = backend.strip().lower()
    if key in {"logging", "log", "null", "none", ""}:
        return LoggingNerfController()
    if key in {"serial", "arduino"}:
        return SerialNerfController(**kwargs)
    raise ValueError(f"unknown nerf backend: {backend!r}")


# ----------------------------------------------------------------- targeting


@dataclass(frozen=True)
class PhoneUser:
    """A person detection paired with a phone they appear to be using."""

    person: Detection
    phone: Detection
    # Horizontal position of the target center, normalized to [0, 1].
    center_x_norm: float

    @property
    def horizontal_error(self) -> float:
        """Signed error from frame center. Negative = target left of center."""
        return self.center_x_norm - 0.5


@dataclass
class TargetingTuning:
    # Firing gates.
    armed_required: bool = True
    center_tolerance: float = 0.12      # |error| under this counts as "on target"
    lock_frames: int = 3                # consecutive on-target frames before fire
    cooldown_s: float = 3.0             # min seconds between shots
    session_fire_cap: int = 20          # hard cap per controller lifetime
    # Aiming.
    max_vyaw: float = 0.6
    yaw_gain: float = 1.2
    # Hardware steering sign. follow.py uses a negative gain on this rig;
    # mirror that here. Flip if your robot turns the wrong way.
    yaw_sign: float = -1.0
    aim_duration_s: float = 0.2


@dataclass(frozen=True)
class TargetingDecision:
    """What the controller decided this frame."""

    has_target: bool
    locked: bool
    fired: bool
    aim_vyaw: float
    reason: str
    target_label: Optional[str] = None


def find_phone_users(observation: Observation) -> list[PhoneUser]:
    """Pair each cell-phone detection with the person holding it.

    A person is "using a phone" if a phone detection's center falls inside
    their bounding box. Returns one PhoneUser per matched phone, sorted by
    how centered the person is (most-centered first).
    """
    persons = [d for d in observation.detections if d.is_human() and _has_box(d)]
    phones = [d for d in observation.detections if _is_phone(d) and _has_box(d)]
    if not persons or not phones:
        return []

    fw = observation.frame_width
    users: list[PhoneUser] = []
    for phone in phones:
        host = _person_containing(phone, persons)
        if host is None:
            continue
        center_x = host.x if host.x is not None else 0.0
        center_norm = _normalize_x(center_x, fw)
        users.append(PhoneUser(person=host, phone=phone, center_x_norm=center_norm))

    users.sort(key=lambda u: abs(u.horizontal_error))
    return users


class TargetingController:
    """Aims at the most-centered phone user and (when armed) fires."""

    def __init__(
        self,
        nerf: Optional[NerfController] = None,
        tuning: Optional[TargetingTuning] = None,
    ) -> None:
        self._nerf = nerf or LoggingNerfController()
        self._t = tuning or TargetingTuning()
        self._consecutive_on_target = 0
        # -inf so the first shot is never blocked by the cooldown window
        # (a 0.0 init collides with now=0.0 in tests and at process start).
        self._last_fire_ts = float("-inf")
        self._fires = 0

    @property
    def nerf(self) -> NerfController:
        return self._nerf

    def arm(self) -> None:
        self._nerf.arm()

    def disarm(self) -> None:
        self._nerf.disarm()
        self._consecutive_on_target = 0

    async def step(self, observation: Observation, *, now: Optional[float] = None) -> TargetingDecision:
        """Process one frame: pick target, compute aim, decide whether to fire."""
        now = time.monotonic() if now is None else now
        users = find_phone_users(observation)

        if not users:
            self._consecutive_on_target = 0
            return TargetingDecision(False, False, False, 0.0, "no phone user in view")

        target = users[0]
        error = target.horizontal_error
        aim_vyaw = self._aim_vyaw(error)
        on_target = abs(error) <= self._t.center_tolerance

        if on_target:
            self._consecutive_on_target += 1
        else:
            self._consecutive_on_target = 0

        locked = self._consecutive_on_target >= self._t.lock_frames

        # Fire gating — every condition must hold.
        if not locked:
            return TargetingDecision(True, False, False, aim_vyaw, "aiming", _label(target))
        if self._t.armed_required and not self._nerf.armed:
            return TargetingDecision(True, True, False, aim_vyaw, "locked but disarmed", _label(target))
        if now - self._last_fire_ts < self._t.cooldown_s:
            return TargetingDecision(True, True, False, aim_vyaw, "cooldown", _label(target))
        if self._fires >= self._t.session_fire_cap:
            return TargetingDecision(True, True, False, aim_vyaw, "session cap reached", _label(target))

        fired = await self._nerf.fire()
        if fired:
            self._last_fire_ts = now
            self._fires += 1
            self._consecutive_on_target = 0  # require re-lock before next shot
        return TargetingDecision(True, True, fired, aim_vyaw, "fired" if fired else "fire failed", _label(target))

    def status(self) -> dict[str, Any]:
        return {
            "nerf": self._nerf.status(),
            "fires": self._fires,
            "consecutive_on_target": self._consecutive_on_target,
        }

    def _aim_vyaw(self, error: float) -> float:
        raw = self._t.yaw_sign * self._t.yaw_gain * error
        return max(-self._t.max_vyaw, min(self._t.max_vyaw, raw))


# ----------------------------------------------------------------- helpers


def _is_phone(d: Detection) -> bool:
    return d.label.lower() in {"cell phone", "cellphone", "phone", "mobile phone"}


def _has_box(d: Detection) -> bool:
    return d.x is not None and d.y is not None and d.width is not None and d.height is not None


def _person_containing(phone: Detection, persons: list[Detection]) -> Optional[Detection]:
    px, py = phone.x or 0.0, phone.y or 0.0
    best: Optional[Detection] = None
    best_area = float("inf")
    for person in persons:
        left = (person.x or 0.0) - (person.width or 0.0) / 2
        right = (person.x or 0.0) + (person.width or 0.0) / 2
        top = (person.y or 0.0) - (person.height or 0.0) / 2
        bottom = (person.y or 0.0) + (person.height or 0.0) / 2
        if left <= px <= right and top <= py <= bottom:
            area = (person.width or 0.0) * (person.height or 0.0)
            # Prefer the tightest containing person (the actual holder, not
            # a big background figure).
            if area < best_area:
                best_area = area
                best = person
    return best


def _normalize_x(x: float, frame_width: Optional[int]) -> float:
    if x <= 1.0:
        return x  # already normalized
    if not frame_width:
        return 0.5  # unknown frame -> assume centered, don't fire blindly
    return max(0.0, min(1.0, x / frame_width))


def _label(user: PhoneUser) -> str:
    return user.person.label
