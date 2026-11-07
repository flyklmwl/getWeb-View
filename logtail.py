"""日志尾部读取 —— 只把需要的最后 N 行读进内存。

为什么不用 f.readlines()：getWeb 的日志会涨到上百 MB，全量读入 + 逐行过滤会让
一个 /logs 请求吃掉几百 MB 内存（2026-11-07 在一台 1.8G 无 swap 的小机上因此触发
OOM，整机僵死 20 分钟）。这里改成从文件尾部按块回溯，内存只与「要的行数 + 单块
大小」相关，与文件总大小无关。
"""

import os

CHUNK = 256 * 1024          # 每轮回读 256KB
SCAN_MAX = 32 * 1024 * 1024  # 最多回溯 32MB，防止在超大文件上白扫


def tail_lines(path, want=200, level="", crawler="", chunk=CHUNK, scan_max=SCAN_MAX):
    """返回文件末尾最后 want 条匹配行，顺序为新 → 旧。

    level / crawler 的匹配口径与旧实现完全一致：
      level   → 行内含 "| INFO    " 这类左对齐 8 宽级别串
      crawler → 行内含 "[crawler]"
    返回值只含匹配到的行；文件不存在/为空时返回 []。
    """
    out = []
    if want is None or want <= 0 or not os.path.isfile(path):
        return out

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        scanned = 0
        carry = b""

        while pos > 0 and len(out) < want and scanned < scan_max:
            step = min(chunk, pos)
            pos -= step
            scanned += step
            f.seek(pos)
            block = f.read(step) + carry
            parts = block.split(b"\n")

            if pos > 0:
                # 第一段可能是被从行中间切开的半行 → 留给下一轮拼
                carry = parts[0]
                body = parts[1:]
            else:
                carry = b""
                body = parts

            for raw in reversed(body):
                if not raw:
                    continue
                line = raw.decode("utf-8", "ignore").rstrip("\r")
                if level and f"| {level:<8}" not in line:
                    continue
                if crawler and f"[{crawler}]" not in line:
                    continue
                out.append(line)
                if len(out) >= want:
                    break

    return out
