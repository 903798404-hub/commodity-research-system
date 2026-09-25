"""Fake Hosted-only task: never accesses production data or credentials."""
from pathlib import Path
import os

assert os.getuid() == 24051 and os.getgid() == 24051
assert Path("/runtime/inputs/request.json").read_text() == "fake-request"
assert Path("/run/secrets/tankan.env").read_text() == "FAKE_TANKAN=only-for-e2e"
assert not Path("/forbidden-host/marker").exists()
assert not Path("/run/secrets/forbidden.env").exists()
assert not Path("/var/run/docker.sock").exists()
assert int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                if line.startswith("CapEff:")), 16) == 0
assert next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith("NoNewPrivs:")) == "1"
try:
    Path("/s1-rootfs-should-be-read-only").write_text("bad")
except OSError:
    pass
else:
    raise AssertionError("root filesystem is writable")
try:
    Path("/runtime/inputs/should-not-write").write_text("bad")
except OSError:
    pass
else:
    raise AssertionError("read-only input mount is writable")
Path("/runtime/snapshots/probe-output").write_text("PASS")
