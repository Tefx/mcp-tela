"""Bounded subprocess regressions for reentrant bridge stop signals."""

import signal
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("phase", ["registration", "forwarding"])
@pytest.mark.parametrize(
    ("first_signal", "nested_signal"),
    [
        (signal.SIGINT, signal.SIGINT),
        (signal.SIGTERM, signal.SIGTERM),
        (signal.SIGINT, signal.SIGTERM),
        (signal.SIGTERM, signal.SIGINT),
    ],
)
def test_bridge_stop_signal_reentry_exits_without_deadlock(
    phase: str, first_signal: int, nested_signal: int,
) -> None:
    """Deliver a second real signal while Event.set holds its nonrecursive lock.

    A separate process bounds the regression: subprocess.run kills and reaps it
    on timeout, so a broken signal handler cannot hang pytest or leak a child.
    Only gateway I/O and attachment persistence are replaced; run_bridge and its
    signal handler, Event.set, interrupt conversion and teardown remain real.
    """
    script = textwrap.dedent(
        """
        import signal
        import sys
        from threading import Event
        from unittest.mock import patch

        from tela.commands import connect_bridge as bridge
        from tela.shell.result import Result

        phase, first, nested = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        nested_delivered = []
        disconnects = []
        flags = []

        def trace(frame, event, arg):
            if (event == 'line' and frame.f_code is Event.set.__code__
                    and frame.f_locals['self']._cond._lock.locked()
                    and not nested_delivered):
                nested_delivered.append(nested)
                # Both cross-signal reentry and a third forwarded signal must
                # return without trying to reacquire Event's held lock.
                signal.raise_signal(nested)
                signal.raise_signal(first)
            return trace

        def register(**kwargs):
            if phase == 'registration':
                signal.raise_signal(first)
                raise AssertionError('registration must be interrupted')
            return Result(value=None)

        def cycle(**kwargs):
            flags.append(kwargs['stop_requested'])
            signal.raise_signal(first)
            raise AssertionError('forwarding must be interrupted')

        def post_json(**kwargs):
            disconnects.append(kwargs['url'])
            # Later signals must not interrupt the in-progress disconnect.
            signal.raise_signal(nested)
            return Result(value=None)

        with (
            patch.object(bridge, '_record_runtime_event_best_effort'),
            patch.object(bridge, '_heartbeat_attachment_best_effort'),
            patch.object(bridge, '_register_bridge_connection', register),
            patch.object(bridge, '_run_bridge_cycle', cycle),
            patch.object(bridge, 'post_json', post_json),
        ):
            sys.settrace(trace)
            try:
                result = bridge.run_bridge(host='127.0.0.1', port=8123, bearer_token='test')
            finally:
                sys.settrace(None)

        assert nested_delivered == [nested], nested_delivered
        assert result.is_err, result
        if phase == 'registration':
            assert result.error == 'INTERRUPT: bridge registration interrupted', result.error
            assert disconnects == [], disconnects
        else:
            assert result.error == 'ATTACH_INTERRUPTED: bridge attach loop interrupted', result.error
            assert len(flags) == 1 and flags[0].is_set()
            assert disconnects == ['http://127.0.0.1:8123/disconnect'], disconnects
        assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, phase, str(int(first_signal)), str(int(nested_signal))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
