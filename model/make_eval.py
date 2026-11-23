"""고정 평가셋 eval.csv 만들기 (k8sapimd/train.md 4-3) — 한 번만 실행해 NFS /csv/eval.csv 로 복사한다.

    python make_eval.py                      # data/seed.csv → data/eval.csv
    python make_eval.py --seed x.csv --out y.csv

seed.csv 를 가이드와 같은 설정(test_size 0.2, random_state 42, stratify)으로 나눈 평가용 92행.
→ 기존 모델(model_v1)의 평가 데이터와 같은 문장이라 기준 지표가 이어진다.
eval.csv 는 바꾸지 않는다 (바꾸면 버전 간 지표 비교가 무의미 — 학습 Job 이 sha256 을 기록).
"""

from __future__ import annotations

import argparse
import csv
import pathlib

import pandas as pd

from train import PARAMS, split_eval


def make_eval(seed: pathlib.Path, out: pathlib.Path) -> int:
    df = pd.read_csv(seed, dtype={"text": str}, keep_default_na=False)
    _, test = split_eval(df, PARAMS)
    test = test.sort_index()   # seed 순서 유지 (사람이 비교하기 쉽게)
    out.parent.mkdir(parents=True, exist_ok=True)
    # seed.csv 와 같은 모양: label 은 따옴표 없이, text 는 따옴표
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC, lineterminator="\n")
        f.write("label,text\n")
        for label, text in zip(test["label"].astype(int), test["text"]):
            w.writerow([label, text])
    return len(test)


def main() -> None:
    here = pathlib.Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="고정 평가셋 eval.csv 생성")
    ap.add_argument("--seed", type=pathlib.Path, default=here / "data" / "seed.csv")
    ap.add_argument("--out", type=pathlib.Path, default=here / "data" / "eval.csv")
    args = ap.parse_args()
    print("saved:", args.out, f"({make_eval(args.seed, args.out)}행)")


if __name__ == "__main__":
    main()
