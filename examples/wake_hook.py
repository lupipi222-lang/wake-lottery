# -*- coding: utf-8 -*-
"""把唤醒抽奖接到自动唤醒上的写法示例。

在你的唤醒程序里，每次「真正的」自动唤醒时调用 lottery_line()，
把返回的那句话拼进发给 AI 的唤醒提示里。
⚠️ 只为保持缓存的保温回合不要调它：每调一次都会把上一次还没抽的机会作废。
"""
import os
import subprocess
import sys
import time

LOTTERY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lottery.py")


def lottery_line():
    eid = "wake_" + time.strftime("%Y%m%d_%H%M%S")
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", LOTTERY, "wake", "--event-id", eid],
                           capture_output=True, text=True, encoding="utf-8", timeout=20)
        out = r.stdout or ""
        if "LOTTERY_WAKE_READY" in out:
            return ("🎰 这次唤醒带了一次抽奖机会。想抽就跑 `python3 %s draw`"
                    "（先看池子，看完再 draw --go）；下次唤醒时这次就作废。" % os.path.abspath(LOTTERY))
        if "LOTTERY_WAKE_LIMIT" in out:
            return "🎰 今天的唤醒抽奖用完了。"
    except Exception as e:  # 抽奖出错不能影响唤醒本身
        print("抽奖签发失败：%s" % e, file=sys.stderr)
    return ""


if __name__ == "__main__":
    print(lottery_line() or "（没有签发）")
