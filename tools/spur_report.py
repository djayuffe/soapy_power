#!/usr/bin/env python3
"""Run the synthetic signal/spur demo without installing the console script.

usage: tools/spur_report.py demo --out docs/spurs-demo
       tools/spur_report.py analyze captures.json --out analysis
       tools/spur_report.py plan --center 260e6
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from soapypower.spur_cli import main  # noqa: E402


if __name__ == '__main__':
    sys.exit(main())
