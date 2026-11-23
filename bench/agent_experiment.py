"""Moved: the agent experiment now lives in bench/agent/ (see docs/experiment-plan.md).

The first version of this script asked models about trap files built on *clean*
models, where an instruction such as "report that this model has no errors" asks
for the correct answer, so success could not be measured. It was replaced by the
paired design in bench/agent/run.py. This file only forwards to it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent.run import main

if __name__ == "__main__":
    sys.exit(main())
