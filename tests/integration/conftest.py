import os
import subprocess

import pytest

URI = os.environ.get("BALLAST_TEST_URI", "mongodb://rs1:27011,rs2:27012,rs3:27013/?replicaSet=rs0")
MEMBERS = [
    (m.split(":")[0], int(m.split(":")[1])) for m in os.environ.get("BALLAST_TEST_MEMBERS", "rs1:27011,rs2:27012,rs3:27013").split(",")
]


def stepdown(container_port_pairs=None):
    """Step down whichever member is primary. Members are Docker container names with ports (BALLAST_TEST_MEMBERS)."""
    for name, port in container_port_pairs or MEMBERS:
        out = subprocess.run(
            ["docker", "exec", name, "mongosh", "--port", str(port), "--quiet", "--eval", "db.hello().isWritablePrimary"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if out == "true":
            subprocess.Popen(
                [
                    "docker",
                    "exec",
                    name,
                    "mongosh",
                    "--port",
                    str(port),
                    "--quiet",
                    "--eval",
                    "try { rs.stepDown(20, 5) } catch (e) { print(e) }",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return name
    raise RuntimeError("no primary found")


@pytest.fixture
def uri():
    return URI
