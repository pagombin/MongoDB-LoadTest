import os
import subprocess

import pytest

URI = os.environ.get("BALLAST_TEST_URI", "mongodb://rs1:27011,rs2:27012,rs3:27013/?replicaSet=rs0")
PRIMARY_CONTAINER = os.environ.get("BALLAST_TEST_PRIMARY_CONTAINER", "rs1")


def stepdown(container_port_pairs=(("rs1", 27011), ("rs2", 27012), ("rs3", 27013))):
    """Step down whichever member is primary."""
    for name, port in container_port_pairs:
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
