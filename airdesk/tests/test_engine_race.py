import threading

from airdesk.messages import MessageLog
from airdesk.radio import ConfigStore, RadioConfig, RadioEngine
from airdesk.store import Store


def test_start_right_after_stop_starts_again(tmp_path):
    """stop() gives up waiting after a while; a worker still winding down must not swallow the next start."""
    engine = RadioEngine(ConfigStore(tmp_path / "radio.json", RadioConfig()), MessageLog(), Store(tmp_path))
    release = threading.Event()
    runs = []

    def run(stop):
        runs.append(stop)
        stop.wait()
        release.wait(2)                      # e.g. blocked in a socket read after being told to stop

    engine._run = run
    engine.start()
    engine._stop.set()                       # what stop() does, without its 5 s join
    engine.start()
    release.set()
    assert len(runs) == 2 and not runs[-1].is_set()
    engine.shutdown()
