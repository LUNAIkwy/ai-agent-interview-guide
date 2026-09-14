# -*- coding: utf-8 -*-
"""
一键按顺序跑完 V1 -> V4。

    python run_all.py --mock          # 离线全跑一遍（推荐第一次这么干）
    python run_all.py                 # 用 .env 里的真实模型
    python run_all.py --max-rounds 6  # 给 V3 限流

命令行参数会原样透传给每个脚本。
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = [
    "v1_pipeline.py",
    "v2_boss_worker.py",
    "v3_deadloop.py",
    "v4_guarded.py",
]


def main() -> None:
    extra = sys.argv[1:]
    for name in SCRIPTS:
        print("\n\n" + "#" * 72)
        print(f"#  {name}")
        print("#" * 72)
        subprocess.run([sys.executable, str(HERE / name), *extra], check=False)


if __name__ == "__main__":
    main()
