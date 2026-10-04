"""六网项目联动排程命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from program_scheduling import MilestoneBaseline


def main() -> None:
    item = MilestoneBaseline(baseline_code='baseline-code-001', program_code='program-code-001', revision=1, state='state-001')
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
