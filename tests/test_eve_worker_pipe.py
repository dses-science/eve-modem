"""The worker's pipe sends are serialized: three threads (frames of >16 KB, half-second
status dicts, log lines) through one multiprocessing Connection must arrive as whole,
unpicklable messages. On Linux an unlocked >16 KB send is two writes and a concurrent send
corrupts the stream (the Haswell Pi 5, 2026-10-20: 'invalid load key', run aborted)."""
import multiprocessing as mp
import os
import sys
import threading

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eve.worker import locked_sender  # noqa: E402


def test_locked_sender_keeps_messages_whole():
    rx, tx = mp.Pipe(duplex=False)
    send = locked_sender(tx)
    n_frames, n_status, n_log = 300, 300, 300
    metric = np.random.default_rng(1).standard_normal(4096).astype(np.float32).tobytes()   # 16 KB + pickle overhead

    def frames():
        for k in range(n_frames):
            send(("frame", k, metric))

    def status():
        for i in range(n_status):
            send(("status", {"phase": f"chunk {i}", "keyed": bool(i % 2), "radio": "B210 " * 20}))

    def logs():
        for i in range(n_log):
            send(("log", f"line {i} " + "x" * 200))

    threads = [threading.Thread(target=f) for f in (frames, status, logs)]
    for t in threads:
        t.start()
    got = {"frame": 0, "status": 0, "log": 0}
    total = n_frames + n_status + n_log
    while sum(got.values()) < total:
        msg = rx.recv()                      # raises on a corrupt stream
        got[msg[0]] += 1
        if msg[0] == "frame":
            assert msg[2] == metric
    for t in threads:
        t.join()
    assert got == {"frame": n_frames, "status": n_status, "log": n_log}


if __name__ == "__main__":
    test_locked_sender_keeps_messages_whole()
    print("PASS")
