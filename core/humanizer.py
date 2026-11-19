import asyncio
import random


class Humanizer:
    def __init__(self, min_delay: float, max_delay: float) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay

    async def sleep_between_swipes(self, has_long_bio: bool = False) -> float:
        mean = (self.min_delay + self.max_delay) / 2
        std_dev = (self.max_delay - self.min_delay) / 4
        delay = random.gauss(mean, std_dev)
        delay = max(self.min_delay, min(self.max_delay, delay))
        if has_long_bio:
            delay += random.uniform(1.5, 4.0)
        if random.random() < 0.10:
            delay += random.uniform(10.0, 20.0)
        await asyncio.sleep(delay)
        return round(delay, 2)
