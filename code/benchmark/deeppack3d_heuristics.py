"""DeepPack3D 启发式部分（从参考项目 agent.py 抽取，避免 TensorFlow 依赖）。"""

from __future__ import annotations

import itertools
from typing import Callable, Iterable, List, Tuple

import numpy as np


def indices(actions) -> List[Tuple[int, int, int]]:
    return [
        (i, j, k)
        for i in range(len(actions))
        for j in range(len(actions[i]))
        for k in range(len(actions[i][j]))
    ]


def bottom_left(actions):
    scores = []
    for i, item in enumerate(actions):
        for j, bin_ in enumerate(item):
            for k, placement in enumerate(bin_):
                _, (x, y, z), (w, h, d), _ = placement
                y = y + h
                x = x + w
                z = z + d
                scores.append(([y, x, z, i, j, k], [i, j, k]))
    order = sorted(range(len(scores)), key=lambda idx: scores[idx][0])
    return scores[order[0]][1]


def best_short_side_fit(actions):
    scores = []
    for i, item in enumerate(actions):
        for j, bin_ in enumerate(item):
            for k, placement in enumerate(bin_):
                _, (x, y, z), (w, h, d), split = placement
                W, H = split.width, split.height
                scores.append(((min(W - w, H - h), i, j, k), [i, j, k]))
    order = sorted(range(len(scores)), key=lambda idx: scores[idx][0])
    return scores[order[0]][1]


def best_area_fit(actions):
    scores = []
    for i, item in enumerate(actions):
        for j, bin_ in enumerate(item):
            for k, placement in enumerate(bin_):
                _, (x, y, z), (w, h, d), split = placement
                W, H = split.width, split.height
                scores.append(((split.volume, min(W - w, H - h), i, j, k), [i, j, k]))
    order = sorted(range(len(scores)), key=lambda idx: scores[idx][0])
    return scores[order[0]][1]


def best_long_side_fit(actions):
    scores = []
    for i, item in enumerate(actions):
        for j, bin_ in enumerate(item):
            for k, placement in enumerate(bin_):
                _, (x, y, z), (w, h, d), split = placement
                W, H = split.width, split.height
                scores.append(((max(W - w, H - h), i, j, k), [i, j, k]))
    order = sorted(range(len(scores)), key=lambda idx: scores[idx][0])
    return scores[order[0]][1]


HEURISTIC_MAP = {
    "bl": bottom_left,
    "baf": best_area_fit,
    "bssf": best_short_side_fit,
    "blsf": best_long_side_fit,
}


class HeuristicAgent:
    """与参考项目 HeuristicAgent 等价的轻量实现。"""

    def __init__(self, heuristic: Callable, env, verbose: bool = False, visualize: bool = False):
        self.env = env
        self.heuristic = heuristic
        self.ep_history = []
        self.verbose = verbose
        self.visualize = visualize

    def select(self, state):
        _, _, actions = state
        return self.heuristic(actions)

    def run(self, max_ep: int = 1, verbose: bool = False):
        iters = range(max_ep)
        for ep in iters:
            if verbose:
                print(f"ep {ep}:")
            state = self.env.reset()
            ep_reward = 0.0
            for _step in itertools.count():
                _, _, actions = state
                if len(actions) == 0:
                    raise RuntimeError("0 actions")
                action = self.select(state)
                yield actions[action[0]][action[1]][action[2]]
                next_state, reward, done = self.env.step(action)
                ep_reward += reward
                if done:
                    break
                state = next_state
            self.ep_history.append(
                (
                    [packer.space_utilization() for packer in self.env.used_packers],
                    self.env.used_bins,
                    ep_reward,
                )
            )
            yield None
