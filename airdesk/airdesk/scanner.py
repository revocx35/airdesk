"""Where to listen next: the airband split into segments, each with an activeness score.

Each segment is as wide as one radio window, so a dwell hears all of it. A segment's score is
voice transmissions per hour of listening, smoothed over about half an hour of listening time.
Listening time is shared out in proportion to score plus a floor, so busy segments are visited
more and quiet ones are still checked now and then. A dwell lasts about 10 s, longer while a
conversation is going on, and never ends in the middle of a transmission. Broadcasts (ATIS, VOLMET)
talk continuously; they neither hold the scanner nor raise their segment's score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .detector import AIRBAND

SEGMENT_W = 1.9e6
TAU_S = 1800.0                 # listening time over which scores are smoothed
EXPLORE_RATE = 2.0             # transmissions/hour every segment is credited with, so none is ever abandoned
INITIAL_RATE = 30.0            # unknown segments start out interesting so all get a first look
BASE_DWELL, MIN_DWELL, MAX_DWELL = 10.0, 6.0, 45.0
LINGER_S = 4.0                 # stay while there was voice in the last few seconds
MAX_FINISH_S = 20.0            # how long to wait for a transmission to end before moving on
LONG_TX_S = 30.0               # longer than this is a broadcast (ATIS, VOLMET): never waited for, never scored


@dataclass
class Segment:
    idx: int
    lo: float
    hi: float
    rate: float = INITIAL_RATE
    listened_s: float = 0.0
    last_visit: float = 0.0
    credit: float = 0.0

    @property
    def center(self) -> float:
        return (self.lo + self.hi) / 2

    def contains(self, f: float) -> bool:
        return self.lo <= f < self.hi or (f == self.hi == AIRBAND[1])

    def public(self, share: float) -> dict:
        return {"idx": self.idx, "lo": self.lo, "hi": self.hi, "rate": round(self.rate, 1),
                "share": round(share, 3), "listened_s": round(self.listened_s), "last_visit": self.last_visit}


def make_segments() -> list[Segment]:
    n = int(round((AIRBAND[1] - AIRBAND[0]) / SEGMENT_W))
    w = (AIRBAND[1] - AIRBAND[0]) / n
    return [Segment(i, AIRBAND[0] + i * w, AIRBAND[0] + (i + 1) * w) for i in range(n)]


def smooth(old: float, observed: float, seconds: float) -> float:
    w = 1 - math.exp(-seconds / TAU_S)
    return old + w * (observed - old)


class Scheduler:
    def __init__(self, segments: list[Segment]):
        self.segments = segments
        self.current: Segment | None = None
        self.dwell_start = 0.0
        self.voice_in_dwell = 0
        self.last_voice = 0.0
        self._tick = None

    def shares(self) -> dict[int, float]:
        weights = {s.idx: s.rate + EXPLORE_RATE for s in self.segments}
        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()}

    def segment_for(self, freq: float) -> Segment | None:
        return next((s for s in self.segments if s.contains(freq)), None)

    def pick(self, now: float, hold: Segment | None = None) -> Segment:
        """Credit every segment with its share of the elapsed time, then go where credit is highest."""
        if self._tick is not None:
            elapsed = now - self._tick
            for s in self.segments:
                s.credit += self.shares()[s.idx] * elapsed
        self._tick = now
        seg = hold or max(self.segments, key=lambda s: (s.credit, -s.last_visit))
        self.current, self.dwell_start, self.voice_in_dwell = seg, now, 0
        return seg

    def should_move(self, now: float, transmitting: bool, holding: bool) -> bool:
        if self.current is None:
            return True
        t = now - self.dwell_start
        if holding:
            return False
        if transmitting and t < MAX_DWELL + MAX_FINISH_S:
            return False                               # never cut a transmission short
        if t < MIN_DWELL:
            return False
        if t < MAX_DWELL and now - self.last_voice < LINGER_S:
            return False                               # a conversation is going on
        return t >= BASE_DWELL or self.voice_in_dwell == 0 and t >= MIN_DWELL and self.current.rate < 1.0

    def voice_heard(self, now: float) -> None:
        self.voice_in_dwell += 1
        self.last_voice = now

    def finish(self, now: float) -> Segment | None:
        """Close the current dwell: update the score and charge the time to the segment."""
        seg = self.current
        if seg is None:
            return None
        dur = max(now - self.dwell_start, 1e-3)
        seg.rate = smooth(seg.rate, self.voice_in_dwell / (dur / 3600.0), dur)
        seg.listened_s += dur
        seg.last_visit = now
        seg.credit -= dur
        self.current = None
        return seg
