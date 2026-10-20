"""The transmit event counter: gr-uhd's async_msgs (underflow, time_error = late packet,
seq_error, burst_ack) counted by gr_blocks.TxEventCounter and turned into the operator
warning by report.tx_events_text. The Haswell Raspberry Pi 5 finding of 2026-10-20: Variant
A's 1.5 MS/s underran the transmitter and the GUI said nothing."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pmt  # noqa: E402

from eve import gr_blocks as G  # noqa: E402
from eve.report import tx_events_text  # noqa: E402


def _msg(*codes):
    """A message shaped like gr-uhd's: (metadata dict . value dict with event_code list)."""
    lst = pmt.PMT_NIL
    for c in codes:
        lst = pmt.list_add(lst, pmt.intern(c))
    d = pmt.make_dict()
    d = pmt.dict_add(d, pmt.intern("event_code"), lst)
    d = pmt.dict_add(d, pmt.intern("channel"), pmt.from_uint64(0))
    return pmt.cons(pmt.make_dict(), d)


def test_tx_event_counter_counts_and_warns():
    c = G.TxEventCounter()
    assert c.snapshot() == {"underflow": 0, "late": 0, "seq_error": 0, "other": 0}
    assert tx_events_text(c.snapshot()) == ""
    c._on_msg(_msg("burst_ack"))                      # ignored
    assert tx_events_text(c.snapshot()) == ""
    c._on_msg(_msg("underflow"))
    for _ in range(5):
        c._on_msg(_msg("time_error"))
    c._on_msg(_msg("underflow_in_packet", "seq_error"))
    assert c.snapshot() == {"underflow": 2, "late": 5, "seq_error": 1, "other": 0}
    txt = tx_events_text(c.snapshot())
    assert txt.startswith("TRANSMITTER UNDERRAN") and "2 underflows" in txt and "5 late packets" in txt
    assert "Variant B" in txt
    c._on_msg(pmt.intern("garbage"))                  # anything odd counts as other, never raises
    assert c.snapshot()["other"] == 1


if __name__ == "__main__":
    test_tx_event_counter_counts_and_warns()
    print("PASS")
