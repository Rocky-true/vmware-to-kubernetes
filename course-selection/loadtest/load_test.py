import argparse
import concurrent.futures
import time

import requests


def main():
    ap = argparse.ArgumentParser(description="选课并发压测")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--course", type=int, default=1)
    ap.add_argument("--students", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=100)
    args = ap.parse_args()

    def select(sid):
        try:
            r = requests.post(
                f"{args.base}/api/select",
                json={"student_id": sid, "course_id": args.course},
                timeout=10,
            )
            d = r.json()
            return d.get("code"), d.get("msg")
        except Exception as e:  # noqa
            return -1, str(e)

    ok = full = dup = err = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for code, msg in ex.map(select, range(1, args.students + 1)):
            if code == 0:
                ok += 1
            elif code == 1:
                full += 1
            elif code == 2:
                dup += 1
            else:
                err += 1
    dt = time.time() - t0

    r = requests.get(f"{args.base}/api/courses/{args.course}").json()
    print(f"请求数={args.students}  成功={ok}  已满={full}  重复={dup}  错误={err}")
    print(f"耗时={dt:.2f}s  吞吐≈{args.students / dt:.0f} req/s")
    print(f"课程[{args.course}] {r['name']}  容量={r['capacity']}  剩余={r['remaining']}")
    if ok + dup == r["capacity"] and r["remaining"] == 0:
        print("[OK] 校验通过：持有名额人数==容量，剩余0，无超卖")
    else:
        print("[WARN] 校验未通过，请检查")


if __name__ == "__main__":
    main()
