#!/usr/bin/env python3
# evens -- report whether an integer is even. Toolbox room tool.
import os
import sys


def is_even(n):
    return n % 2 == 0


def main(argv):
    if len(argv) != 1:
        print("usage: evens <integer>")
        return 2
    try:
        n = int(argv[0])
    except ValueError:
        print("not an integer: %s" % argv[0])
        return 2
    verdict = "even" if is_even(n) else "odd"
    print("%d is %s" % (n, verdict))
    # durable result, temp-then-rename per the tool contract
    os.makedirs("/scratch/result", exist_ok=True)
    tmp = "/scratch/result/answer.txt.tmp"
    with open(tmp, "w") as f:
        f.write("%d %s\n" % (n, verdict))
    os.replace(tmp, "/scratch/result/answer.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
