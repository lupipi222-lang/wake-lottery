"""从相册里随机翻一张，连当时写的备注一起看。

相册就是一个文件夹加一份索引：
    album/
      index.json      每一张一条：{"file": "...", "date": "...", "note": "..."}
      2026-09-28_汤.jpg
      ...

用法：
    python3 examples/album_pick.py            随机翻一张
    python3 examples/album_pick.py 3          看第 3 张（从 1 数）
    python3 examples/album_pick.py --no-private   不抽标了 private 的

相册默认在 WAKE_LOTTERY_HOME（没设就是仓库根目录）下的 album/。
"""
import json
import os
import random
import sys
from pathlib import Path

HOME = Path(os.environ.get("WAKE_LOTTERY_HOME") or Path(__file__).resolve().parent.parent)
ALBUM = HOME / "album"
INDEX = ALBUM / "index.json"


def main(argv):
    if not INDEX.exists():
        print("还没有相册：先建 %s，每存一张照片就往里写一条备注。" % INDEX)
        return 1
    items = json.loads(INDEX.read_text(encoding="utf-8"))
    if "--no-private" in argv:
        items = [it for it in items if not it.get("private")]
    if not items:
        print("相册是空的。")
        return 1
    nums = [a for a in argv if a.isdigit()]
    if nums:
        i = int(nums[0]) - 1
        if not 0 <= i < len(items):
            print("只有 %d 张。" % len(items))
            return 1
    else:
        i = random.randrange(len(items))
    it = items[i]
    print("第 %d 张（共 %d 张）· %s" % (i + 1, len(items), it.get("date", "")))
    print()
    print(it.get("note", "（这张没写备注）"))
    print()
    print("文件：%s" % (ALBUM / it.get("file", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
