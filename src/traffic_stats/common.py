"""Bounded JSON and cancellable subprocess operations shared by both commands."""
import errno
import json
import math
import os
import selectors
import subprocess
import time

MAX_JSON = 16 * 1024 * 1024
UINT64_MAX = (1 << 64) - 1
INT64_MIN = -(1 << 63)


def _integer(text):
    value = int(text)
    if not INT64_MIN <= value <= UINT64_MAX:
        raise ValueError("JSON integer is outside the supported range")
    return value


def _floating(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _constant(text):
    raise ValueError("Invalid JSON constant: " + text)


def loads(data, max_size=MAX_JSON):
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > max_size or b"\0" in data:
        raise ValueError("JSON input is too large or contains a NUL byte")
    try:
        result = json.loads(data, parse_int=_integer, parse_float=_floating,
                            parse_constant=_constant)
    except (RecursionError, UnicodeError) as error:
        raise ValueError("Invalid JSON input") from error
    if result is None:
        raise ValueError("JSON null is not a document")
    return result


def read_json(stream, max_size=MAX_JSON):
    return loads(stream.read(max_size + 1), max_size)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def run(argv, input_data=None, capture=True, cancel=None, timeout=15, max_output=MAX_JSON):
    """Use pipes with bounded reads so a bad child cannot exhaust router memory."""
    if cancel and cancel():
        raise InterruptedError(errno.EINTR, "Command cancelled")
    if isinstance(input_data, str):
        input_data = input_data.encode("utf-8")
    pending = memoryview(input_data or b"")
    output = bytearray()
    count = 0
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE) as child, selectors.DefaultSelector() as selector:
        os.set_blocking(child.stdout.fileno(), False)
        selector.register(child.stdout, selectors.EVENT_READ)
        if child.stdin is not None:
            if pending:
                os.set_blocking(child.stdin.fileno(), False)
                selector.register(child.stdin, selectors.EVENT_WRITE)
            else:
                child.stdin.close()
        try:
            while selector.get_map() or child.poll() is None:
                if cancel and cancel():
                    raise InterruptedError(errno.EINTR, "Command cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(errno.ETIMEDOUT, "Command timed out: " + argv[0])
                for key, event in selector.select(min(remaining, 0.1)):
                    if event & selectors.EVENT_READ:
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        count += len(data)
                        if count > max_output:
                            raise OSError(errno.EFBIG, "Command output limit exceeded: " + argv[0])
                        if capture:
                            output.extend(data)
                    elif event & selectors.EVENT_WRITE:
                        try:
                            written = os.write(key.fd, pending[:65536])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[len(pending):]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
            if child.returncode:
                raise OSError(errno.EIO, "Command failed: %s (exit %s)" % (argv[0], child.returncode))
        except BaseException:
            if child.poll() is None:
                child.kill()
            child.wait()
            raise
    return bytes(output)
