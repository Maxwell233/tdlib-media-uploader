"""Drain child pipes into bounded byte buffers without blocking either stream."""
from __future__ import annotations

import codecs
import threading


def bounded_communicate(process, input_data, limit, *, encoding=None, errors=None):
    """Retain at most limit bytes per pipe; discard overflow while draining.

    Pipes must be binary. Readers own and close their pipes, including when
    termination interrupts a child blocked on writing one of the streams.
    """
    buffers = [None, None]
    failures = []

    def drain(index, pipe):
        if pipe is None:
            return
        output = bytearray()
        buffers[index] = output
        try:
            while chunk := pipe.read(65536):
                remaining = max(0, limit - len(output))
                output.extend(chunk[:remaining])
        except OSError as exc:
            failures.append(exc)
        finally:
            pipe.close()

    readers = [threading.Thread(target=drain, args=(index, pipe), daemon=True)
               for index, pipe in enumerate((process.stdout, process.stderr)) if pipe is not None]
    for reader in readers:
        reader.start()
    if process.stdin is not None:
        try:
            if input_data is not None:
                process.stdin.write(input_data)
        except BrokenPipeError:
            pass
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
    for reader in readers:
        reader.join()
    process.wait()
    if failures:
        raise failures[0]
    result = []
    for buffer in buffers:
        value = bytes(buffer) if buffer is not None else None
        if value is not None and encoding:
            # An incomplete final character caused by the byte limit is omitted.
            value = codecs.getincrementaldecoder(encoding)(errors or "strict").decode(value, final=False)
            value = value.replace("\r\n", "\n").replace("\r", "\n")
        result.append(value)
    return tuple(result)
