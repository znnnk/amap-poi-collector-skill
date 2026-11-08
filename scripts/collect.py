# -*- coding: utf-8 -*-
"""单行入口：``python scripts/collect.py -k 汽修厂 -r 海口市``

存在的意义只有一个：让「不装包、不 pip install、直接从技能目录跑」成立。
采集逻辑都在同目录的 ``amap_poi`` 包里，这里只补 ``sys.path`` 然后转交 CLI。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from amap_poi.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
