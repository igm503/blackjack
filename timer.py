from collections import defaultdict
from contextlib import contextmanager

import time


class LoopTimer:
    def __init__(self, print_interval=1):
        self.timings = defaultdict(float)
        self.total_count = 0
        self.count = 0
        self.separate_counts = defaultdict(int)
        self.last_time = 0
        self.start_time = 0
        self.last_report = 0
        self.print_interval = print_interval

    def start(self):
        self.start_time = time.time()
        self.last_report = self.start_time
        self.last_time = self.start_time

    def mark(self):
        self.last_time = time.time()

    def time(self, name, separate_count=False):
        elapsed = time.time() - self.last_time
        self.timings[name] += elapsed
        self.mark()
        if separate_count:
            self.separate_counts[name] += 1
        return elapsed

    def loop(self):
        self.count += 1
        self.total_count += 1
        self.mark()
        if self.print_interval and (self.last_time - self.last_report) > self.print_interval:
            self.print()
            self.last_report = self.last_time

    @contextmanager
    def timing(self, name, separate_count=False):
        self.mark()
        yield
        self.time(name, separate_count)

    def print(self, reset=True):
        if not self.timings:
            return
        report_strs = [f"Iterations: {self.total_count}"]
        report_strs.append(f"it/sec: {self.count / (self.last_time - self.start_time):.1f}")
        for name, total_time in self.timings.items():
            count = self.separate_counts[name] if name in self.separate_counts else self.count
            avg_time = total_time / count if count > 0 else 0
            report_strs.append(f"{name}: {avg_time * 1000:.0f}ms")
        report_str = " | ".join(report_strs)
        print(report_str)
        if reset:
            self.split()

    def split(self):
        self.timings.clear()
        self.separate_counts.clear()
        self.count = 0
        self.start()

    def reset(self):
        self.split()
        self.total_count = 0
