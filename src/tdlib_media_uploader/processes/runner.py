"""Bounded external-process execution with cooperative cancellation."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

from ..core.concurrency import CancellationRequested, is_cancelled


ProcessCancelled = CancellationRequested


def _truncate_output(value, limit: int | None):
    if limit is None or value is None:
        return value
    limit = max(0, int(limit))
    if isinstance(value, bytes):
        return value[:limit]
    return str(value)[:limit]


def run_cancellable_process(
    command,
    *,
    cancel_event=None,
    cancel_token=None,
    timeout=None,
    terminate_grace_seconds: float = 0.5,
    max_output_bytes: int | None = None,
    **kwargs,
):
    """Run *command* while draining pipes and observing cancellation.

    When neither cancellation mechanism is supplied this delegates directly
    to ``subprocess.run`` so existing callers can retain its exact behavior.
    When cancellation is enabled, one daemon worker owns ``communicate`` for
    the complete child lifetime.  That continuously drains stdout/stderr and
    avoids the classic deadlock caused by polling a child while its pipe is
    full.
    """

    if cancel_event is None and cancel_token is None:
        return subprocess.run(command, timeout=timeout, **kwargs)
    if is_cancelled(cancel_token, cancel_event):
        raise ProcessCancelled("外部进程已取消")

    input_data = kwargs.pop("input", None)
    check = bool(kwargs.pop("check", False))
    capture_output = bool(kwargs.pop("capture_output", False))
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout/stderr 参数不能与 capture_output 同时使用")
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if input_data is not None:
        if "stdin" in kwargs and kwargs["stdin"] is not subprocess.PIPE:
            raise ValueError("stdin 参数不能与 input 同时使用")
        kwargs.setdefault("stdin", subprocess.PIPE)

    # Give POSIX children a private process group so helpers they spawn do not
    # keep the parent's pipes open after cancellation.  An explicit caller
    # setting always wins.
    process_group_enabled = os.name != "nt" and kwargs.get("start_new_session", True)
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    process = subprocess.Popen(command, **kwargs)

    result_holder: dict[str, object] = {}
    communication_done = threading.Event()

    def communicate_worker() -> None:
        try:
            result_holder["result"] = process.communicate(input=input_data)
        except BaseException as exc:  # propagate subprocess errors to caller
            result_holder["error"] = exc
        finally:
            communication_done.set()

    worker = threading.Thread(
        target=communicate_worker,
        name="tdlib-process-communicate",
        daemon=True,
    )
    worker.start()
    started = time.monotonic()
    stop_reason: str | None = None

    def stop_child() -> None:
        if process.poll() is not None:
            return
        if process_group_enabled:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    process.terminate()
                except OSError:
                    pass
        else:
            try:
                process.terminate()
            except OSError:
                pass

        grace = max(0.0, float(terminate_grace_seconds))
        deadline = time.monotonic() + grace
        while process.poll() is None and time.monotonic() < deadline:
            communication_done.wait(min(0.05, max(0.0, deadline - time.monotonic())))
        if process.poll() is None:
            if process_group_enabled:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        process.kill()
                    except OSError:
                        pass
            else:
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=max(1.0, grace + 1.0))
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass

    while not communication_done.wait(0.05):
        if is_cancelled(cancel_token, cancel_event):
            stop_reason = "cancelled"
            stop_child()
            break
        if timeout is not None and time.monotonic() - started >= float(timeout):
            stop_reason = "timeout"
            stop_child()
            break

    if stop_reason is not None:
        # terminate/kill is followed by a bounded join.  The communication
        # thread is the only owner of the pipes, so it is never concurrently
        # called a second time from this polling thread.
        worker.join(timeout=max(1.0, float(terminate_grace_seconds) + 1.0))
        stdout, stderr = result_holder.get("result", (None, None))
        stdout = _truncate_output(stdout, max_output_bytes)
        stderr = _truncate_output(stderr, max_output_bytes)
        if stop_reason == "cancelled":
            raise ProcessCancelled("外部进程已取消")
        error = subprocess.TimeoutExpired(command, timeout)
        error.output = stdout
        error.stderr = stderr
        raise error

    worker.join(timeout=1.0)
    if "error" in result_holder:
        raise result_holder["error"]
    stdout, stderr = result_holder.get("result", (None, None))
    stdout = _truncate_output(stdout, max_output_bytes)
    stderr = _truncate_output(stderr, max_output_bytes)
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and completed.returncode:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return completed


def run_process(command, **kwargs):
    """Compatibility alias for the extracted process boundary."""

    return run_cancellable_process(command, **kwargs)


def run_external_process(command, **kwargs):
    """Descriptive alias used by media-tool adapters."""

    return run_cancellable_process(command, **kwargs)


__all__ = [
    "ProcessCancelled",
    "run_cancellable_process",
    "run_external_process",
    "run_process",
]
