import pathlib
import sys

# model/ 를 import 경로에 (train.py, serve.py)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
