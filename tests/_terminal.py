"""In-memory terminal transport for deterministic cell and input tests."""

import asyncio

class MemoryTerminal:
    """A terminal byte transport with real cell dimensions and no native modes."""

    def __init__(self, columns=160, rows=45):
        self.facts = columns, rows, columns * 8, rows * 16
        self.incoming = b""
        self.output = []
        self.entered = self.exited = 0

    def enter(self):
        self.entered += 1

    def exit(self):
        self.exited += 1

    def read(self, timeout=0):
        data, self.incoming = self.incoming, b""
        return data

    def write(self, data):
        self.output.append(bytes(data))

    def dimensions(self):
        return self.facts

async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.005)
