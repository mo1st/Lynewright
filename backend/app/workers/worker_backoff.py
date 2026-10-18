from dataclasses import dataclass


@dataclass
class WorkerBackoff:
    initial_seconds: float = 1.0
    maximum_seconds: float = 30.0
    multiplier: float = 2.0
    current_seconds: float | None = None

    def __post_init__(self) -> None:
        self.initial_seconds = max(.1, self.initial_seconds)
        self.maximum_seconds = max(self.initial_seconds, self.maximum_seconds)
        self.current_seconds = self.initial_seconds

    def next_delay(self) -> float:
        delay = float(self.current_seconds or self.initial_seconds)
        self.current_seconds = min(self.maximum_seconds, delay * self.multiplier)
        return delay

    def reset(self) -> None:
        self.current_seconds = self.initial_seconds
