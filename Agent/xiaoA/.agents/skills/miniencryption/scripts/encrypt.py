#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""miniEncryption 辅助脚本：阿拉伯数字与中文大写数字的逐字符互转。

用法示例：
    python encrypt.py --text "订单 A2024"           # 打印密文
    python encrypt.py --file in.txt                 # 打印文件的密文
    python encrypt.py --file in.txt --out out.txt   # 写文件，并打印统计
    python encrypt.py --decrypt --file out.txt      # 反向还原
    echo 023 | python encrypt.py                    # 从标准输入读

退出码：0 成功；1 参数或读写错误。
"""

import argparse
import sys

DIGITS = "0123456789"
HALF_TO_CN = "零壹贰叁肆伍陆柒捌玖"
FULL_TO_CN = "零壹贰叁肆伍陆柒捌玖"

# 编码表：ASCII 数字 + 全角数字 → 中文大写数字
ENCODE_MAP = {}
for _i, _ch in enumerate(DIGITS):
    ENCODE_MAP[_ch] = HALF_TO_CN[_i]
for _i, _code in enumerate(range(0xFF10, 0xFF1A)):
    ENCODE_MAP[chr(_code)] = FULL_TO_CN[_i]

# 解码表：中文大写数字 → ASCII 数字（显式构造，避免被全角键覆盖）
DECODE_MAP = {HALF_TO_CN[i]: DIGITS[i] for i in range(10)}
DECODE_MAP["〇"] = "0"  # 兼容「〇」写法


def transform(text, decode=False):
    """逐字符替换，返回 (结果, 被替换的字符数)。"""
    mapping = DECODE_MAP if decode else ENCODE_MAP
    out = []
    hits = 0
    for ch in text:
        if ch in mapping:
            out.append(mapping[ch])
            hits += 1
        else:
            out.append(ch)
    return "".join(out), hits


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把阿拉伯数字逐字符替换为中文大写数字（或反向还原）"
    )
    parser.add_argument("--text", help="直接给出要处理的文本")
    parser.add_argument("--file", help="从该文件读取文本（UTF-8）")
    parser.add_argument("--out", help="结果写入该文件；不给则打印到标准输出")
    parser.add_argument("--decrypt", action="store_true", help="反向还原：中文数字转回阿拉伯数字")
    parser.add_argument("--quiet", action="store_true", help="写文件时不打印统计信息")
    args = parser.parse_args(argv)

    if args.text is not None and args.file:
        print("错误：--text 和 --file 只能选一个", file=sys.stderr)
        return 1

    if args.text is not None:
        source = args.text
    elif args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                source = f.read()
        except OSError as exc:
            print("读取文件失败：%s" % exc, file=sys.stderr)
            return 1
    else:
        source = sys.stdin.read()

    result, hits = transform(source, decode=args.decrypt)
    action = "还原" if args.decrypt else "替换"

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(result)
        except OSError as exc:
            print("写入文件失败：%s" % exc, file=sys.stderr)
            return 1
        if not args.quiet:
            print("已写入 %s（%s %d 个字符）" % (args.out, action, hits))
    else:
        sys.stdout.write(result)
        if not result.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
