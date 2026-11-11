#!/usr/bin/env python3
"""virtual_clock.py -- simulated client clock with configurable offset and drift.

Intended location in the repository: client/virtual_clock.py

The real operating-system clock is never modified. The virtual clock is a
software clock layered on top of two real clocks:

* ``time.time()``      -- the *reference* Unix-epoch time. This is the same time
                          base the server reports in T2/T3 (when client and
                          server share a kernel, as on Mininet, or are NTP-synced).
* ``time.monotonic()`` -- elapsed time. Drift accumulates against this clock so
                          the virtual clock advances smoothly even if the OS wall
                          clock is stepped.

Model::

    virtual_time = reference_time + offset + accumulated_drift

    accumulated_drift = drift_ppm * 1e-6 * (monotonic elapsed seconds)

Sign conventions (this module)
------------------------------
* offset  > 0 : the virtual clock is AHEAD of the reference clock.
* drift   > 0 : the virtual clock runs FASTER than the reference clock.
* ``get_offset()`` returns ``virtual - reference``.

The synchronization client uses the opposite sign for theta
(``theta = server - client``); ``get_offset_to_reference()`` returns that
convention (``reference - virtual``) and ``adjust_clock(theta)`` expects it.
"""

import math
import threading
import time


class VirtualClock:
    """A simulated clock that differs from the reference clock.

    Parameters
    ----------
    initial_offset : float
        Initial offset in seconds (virtual - reference). Positive = ahead.
    drift_ppm : float
        Clock drift in parts per million. Positive = runs faster.
    """

    # A clock whose rate is <= 0 would stop or run backwards.
    _MIN_DRIFT_PPM = -1e6

    def __init__(self, initial_offset=0.0, drift_ppm=0.0):
        self._validate_offset(initial_offset)
        self._validate_drift(drift_ppm)
        self._lock = threading.RLock()
        self._initial_offset = float(initial_offset)
        self._initial_drift_ppm = float(drift_ppm)
        self._start(self._initial_offset, self._initial_drift_ppm)

    # ------------------------------------------------------------------ #
    # validation / internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_offset(value):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("offset must be a finite number of seconds, got %r" % (value,))

    @classmethod
    def _validate_drift(cls, value):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("drift must be a finite number of ppm, got %r" % (value,))
        if value <= cls._MIN_DRIFT_PPM:
            raise ValueError("drift must be greater than -1e6 ppm (clock would stop)")

    def _start(self, offset, drift_ppm):
        """(Re)initialise internal state. Caller must hold the lock or be in __init__."""
        self._offset = float(offset)        # offset component (initial + set + corrections)
        self._drift_acc = 0.0               # drift folded in by earlier rebases
        self._drift_ppm = float(drift_ppm)
        self._anchor_mono = time.monotonic()

    def _segment_drift(self, mono_now):
        """Drift accumulated since the last anchor, in seconds."""
        return self._drift_ppm * 1e-6 * (mono_now - self._anchor_mono)

    def _rebase(self, mono_now):
        """Fold the drift accumulated so far into ``_drift_acc`` and restart the
        drift segment, so that changing the drift rate or offset never causes
        a jump in the virtual time."""
        self._drift_acc += self._segment_drift(mono_now)
        self._anchor_mono = mono_now

    def _offset_at(self, mono_now):
        """Total offset (virtual - reference) at the given monotonic instant."""
        return self._offset + self._drift_acc + self._segment_drift(mono_now)

    # ------------------------------------------------------------------ #
    # reading the clock
    # ------------------------------------------------------------------ #
    @staticmethod
    def reference_time():
        """Reference Unix-epoch time (``time.time()``), the server's time base."""
        return time.time()

    def get_time_and_offset(self):
        """Return ``(virtual_time, offset)`` from a single consistent reading.

        ``offset`` is exactly ``virtual_time - reference_time`` for the same
        instant, so callers can record the true error of a timestamp at the
        moment it was taken.
        """
        with self._lock:
            mono_now = time.monotonic()
            ref = time.time()
            offset = self._offset_at(mono_now)
            return ref + offset, offset

    def get_time(self):
        """Current virtual Unix-epoch time in seconds."""
        return self.get_time_and_offset()[0]

    def get_offset(self):
        """Actual simulated offset right now: ``virtual - reference`` (seconds).

        Positive means the virtual clock is ahead of the reference clock.
        """
        with self._lock:
            return self._offset_at(time.monotonic())

    def get_offset_to_reference(self):
        """Offset in the synchronization convention: ``reference - virtual``.

        Positive means the virtual clock is BEHIND the reference. This is the
        quantity the client's estimated theta (``server - client``) approximates.
        """
        return 0.0 - self.get_offset()      # '0.0 -' avoids returning -0.0

    # ------------------------------------------------------------------ #
    # modifying the clock
    # ------------------------------------------------------------------ #
    def set_offset(self, offset):
        """Set the total current offset (``virtual - reference``) to ``offset``.

        Any drift accumulated so far is discarded; the drift rate is unchanged
        and keeps accumulating from now on.
        """
        self._validate_offset(offset)
        with self._lock:
            self._offset = float(offset)
            self._drift_acc = 0.0
            self._anchor_mono = time.monotonic()

    def set_drift(self, drift_ppm):
        """Change the drift rate (ppm). Drift accumulated so far is preserved."""
        self._validate_drift(drift_ppm)
        with self._lock:
            self._rebase(time.monotonic())
            self._drift_ppm = float(drift_ppm)

    def adjust_clock(self, correction):
        """Apply a synchronization correction, in seconds.

        ``correction`` is the estimated theta (``server - client``): it is ADDED
        to the virtual time. A positive value moves the clock forward (it was
        behind), a negative value moves it backward (it was ahead). The drift
        rate is not changed, so a drifting clock will diverge again over time.

        Returns the new total offset (``virtual - reference``).
        """
        self._validate_offset(correction)
        with self._lock:
            self._rebase(time.monotonic())
            self._offset += float(correction)
            return self._offset + self._drift_acc

    def reset(self):
        """Restore the initial offset and drift and restart accumulation."""
        with self._lock:
            self._start(self._initial_offset, self._initial_drift_ppm)

    # ------------------------------------------------------------------ #
    # drift information
    # ------------------------------------------------------------------ #
    def get_drift(self):
        """Current drift rate in ppm (positive = runs faster)."""
        with self._lock:
            return self._drift_ppm

    def get_drift_info(self):
        """Return a dict describing the drift state."""
        with self._lock:
            mono_now = time.monotonic()
            return {
                "drift_ppm": self._drift_ppm,
                "drift_rate_s_per_s": self._drift_ppm * 1e-6,
                "accumulated_drift_s": self._drift_acc + self._segment_drift(mono_now),
                "current_offset_s": self._offset_at(mono_now),
            }

    def __repr__(self):
        info = self.get_drift_info()
        return "VirtualClock(offset=%+.6fs, drift=%+.3fppm, accumulated_drift=%+.6fs)" % (
            info["current_offset_s"], info["drift_ppm"], info["accumulated_drift_s"])
