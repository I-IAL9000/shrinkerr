"""Pause-on-stream and a job's next ffmpeg (v0.10.0).

The VMAF target's sample encodes are registered like the encode itself, so a
stream freezes them. A frozen sample that ends (its timeout) left the job
marked paused, and the freeze loop skips paused jobs: the encode that
followed ran through the rest of the stream. A new ffmpeg for a paused job
is frozen at once, and resumed with the others.
"""
import signal

from backend.queue import QueueWorker


class _Proc:
    returncode = None

    def __init__(self):
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.signals.append(signal.SIGKILL)


def _worker():
    w = QueueWorker.__new__(QueueWorker)
    w._active_procs, w._cancel_flags, w._stream_paused_jobs = {}, set(), set()
    return w


def test_a_new_ffmpeg_for_a_stream_paused_job_is_frozen_and_resumed():
    w = _worker()
    sample = _Proc()
    w._register_proc(7, sample)
    assert w._freeze_active_procs("Plex") == 1 and sample.signals == [signal.SIGSTOP]

    encode = _Proc()  # the sample ended (timed out); the encode starts
    w._register_proc(7, encode)
    assert encode.signals == [signal.SIGSTOP]

    assert w._resume_paused_procs() == 1 and encode.signals == [signal.SIGSTOP, signal.SIGCONT]


def test_a_new_ffmpeg_for_a_running_job_is_left_alone():
    w = _worker()
    proc = _Proc()
    w._register_proc(7, proc)
    assert proc.signals == [] and w._active_procs[7] is proc


def test_a_new_ffmpeg_for_a_cancelled_job_is_killed():
    w = _worker()
    w._cancel_flags.add(7)
    w._stream_paused_jobs.add(7)
    proc = _Proc()
    w._register_proc(7, proc)
    assert proc.signals == [signal.SIGKILL]
