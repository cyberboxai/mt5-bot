#!/usr/bin/env python3
"""Static architecture audit: LOC, internal import graph, cycles, forbidden imports and globals."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("mt5bot", "panel")


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve(current: str, is_pkg: bool, node: ast.ImportFrom) -> list[str]:
    base = current.split(".")
    if node.level:
        base = base if is_pkg else base[:-1]
        base = base[: len(base) - (node.level - 1)]
    else:
        base = []
    prefix = ".".join(base + ([node.module] if node.module else []))
    return [prefix] + [f"{prefix}.{a.name}" for a in node.names]


def scan() -> dict:
    files = [p for pkg in PACKAGES for p in (ROOT / pkg).rglob("*.py")]
    modules = {module_name(p): p for p in files}
    graph: dict[str, set[str]] = {m: set() for m in modules}
    top_level: dict[str, set[str]] = {m: set() for m in modules}
    loc, mt5, globals_, blocking = {}, [], [], []
    for name, path in modules.items():
        text = path.read_text(encoding="utf-8")
        loc[name] = sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))
        tree = ast.parse(text)
        toplevel_nodes = {id(n) for n in tree.body}
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = resolve(name, path.name == "__init__.py", node)
            for target in targets:
                if target.split(".")[0] == "MetaTrader5":
                    mt5.append(f"{name}:{node.lineno}")
                if target in modules and target != name:
                    graph[name].add(target)
                    if id(node) in toplevel_nodes:
                        top_level[name].add(target)
            if isinstance(node, ast.Global):
                globals_.append(f"{name}:{node.lineno}:{','.join(node.names)}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "sleep":
                blocking.append(f"{name}:{node.lineno}")
    return {"loc": loc, "graph": {k: sorted(v) for k, v in graph.items()}, "cycles": cycles(top_level),
            "mt5_imports": mt5, "global_statements": globals_, "sleep_calls": blocking}


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index, low, stack, on, out = {}, {}, [], set(), []
    counter = [0]

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    sys.setrecursionlimit(5000)
    for v in graph:
        if v not in index:
            strong(v)
    return out


if __name__ == "__main__":
    report = scan()
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(report, indent=1), encoding="utf-8")
    ranked = sorted(report["loc"].items(), key=lambda kv: -kv[1])
    print("total LOC", sum(report["loc"].values()), "modules", len(report["loc"]))
    print("top", ranked[:12])
    print("cycles", report["cycles"])
    print("mt5", report["mt5_imports"])
    print("global", report["global_statements"])
    print("sleep", report["sleep_calls"])
