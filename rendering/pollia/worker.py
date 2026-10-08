"""App worker. stdout is JSON lines; diagnostics belong on stderr."""
import argparse
import json
import mmap
import os
import sys
import time

from .session import RenderSession, MetalBackendError

PROTOCOL = 1
MAX_COMMAND_BYTES = 1024 * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-file", required=True)
    parser.add_argument("--variant", default="metal_rgb_polarized")
    args = parser.parse_args()
    # Redirect the OS descriptor too: custom C++ plugins can write directly to
    # stdout, bypassing Python's sys.stdout and corrupting a JSON-only pipe.
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr

    def reply(message):
        protocol_out.write(json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n")
        protocol_out.flush()

    shared = None
    capacity = 0
    frame = None
    generation = 0
    try:
        session = RenderSession(args.variant)
        with open(args.shared_file, "r+b") as shared_file:
            reply(dict(event="ready", protocol=PROTOCOL, variant=session.variant))
            while True:
                line = sys.stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_COMMAND_BYTES or not line.endswith(b"\n"):
                    raise ValueError("Oversized or incomplete command")
                request_id = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("Command must be an object")
                    request_id = request.get("id")
                    if type(request_id) is not int or request_id < 1:
                        raise ValueError("Command needs a positive integer id")
                    command = request.get("command")
                    if command == "load":
                        parameters = request.get("parameters", {})
                        if not isinstance(parameters, dict):
                            raise ValueError("parameters must be an object")
                        session.load_file(request["path"], **parameters)
                        frame = None
                        result = dict(loaded=True)
                    elif command == "render":
                        frame = None
                        frame = session.render(spp=request.get("spp", 16),
                                               seed=request.get("seed", 0),
                                               sensor=request.get("sensor", 0),
                                               output=request.get("output", "preview"))
                        generation += 1
                        result = dict(generation=generation, shape=list(frame.pixels.shape),
                                      channel_names=frame.channel_names,
                                      bytes=frame.pixels.nbytes, dtype="float32-le",
                                      render_ms=frame.render_ms, readback_ms=frame.readback_ms,
                                      kernel_count=frame.kernel_count, variant=session.variant)
                    elif command == "publish":
                        if frame is None or request.get("generation") != generation:
                            raise ValueError("No matching frame to publish")
                        new_capacity = request.get("capacity")
                        if type(new_capacity) is not int or new_capacity < frame.pixels.nbytes:
                            raise ValueError("Shared buffer is too small")
                        if shared is None or new_capacity != capacity:
                            if shared is not None:
                                shared.close()
                            shared = None
                            shared = mmap.mmap(shared_file.fileno(), new_capacity)
                            capacity = new_capacity
                        start = time.perf_counter()
                        shared[:frame.pixels.nbytes] = memoryview(frame.pixels).cast("B")
                        result = dict(generation=generation,
                                      shared_ms=(time.perf_counter() - start) * 1000)
                    elif command == "ping":
                        result = dict(variant=session.variant)
                    elif command == "shutdown":
                        reply(dict(id=request_id, ok=True, result=dict(stopped=True)))
                        break
                    else:
                        raise ValueError("Unknown command")
                    reply(dict(id=request_id, ok=True, result=result))
                except Exception as error:
                    reply(dict(id=request_id, ok=False,
                               error=dict(type=type(error).__name__, message=str(error))))
                    if isinstance(error, MetalBackendError):
                        return 1
    except Exception as error:
        reply(dict(event="fatal", error=dict(type=type(error).__name__, message=str(error))))
        return 1
    finally:
        if shared is not None:
            shared.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
