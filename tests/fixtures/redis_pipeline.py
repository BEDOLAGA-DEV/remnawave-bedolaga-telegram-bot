"""Pipeline protocol adapter for sequential in-memory test clients.

This does not emulate Redis isolation or Lua. Atomicity tests use real Valkey.
"""


class SequentialPipelineMixin:
    def pipeline(self, *, transaction=True):
        client = self

        class Pipeline:
            def __init__(self):
                self.commands = []

            def setex(self, *args):
                self.commands.append(args)

            async def execute(self):
                return [await client.setex(*args) for args in self.commands]

        return Pipeline()
