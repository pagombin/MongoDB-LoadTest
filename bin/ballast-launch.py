"""Starts Ballast with a host Python installed by scripts/install.sh. Run with python -I, so neither the
current directory nor PYTHON* variables affect imports; the package directory comes from BALLAST_SITE.
The supervisor starts the server through this file too (BALLAST_LAUNCHER)."""

import os
import runpy
import sys

site = os.environ.get("BALLAST_SITE", "")
if site:
    sys.path.insert(0, site)
os.environ["BALLAST_LAUNCHER"] = os.path.abspath(__file__)
sys.argv[0] = "ballast"
runpy.run_module("ballast", run_name="__main__", alter_sys=True)
