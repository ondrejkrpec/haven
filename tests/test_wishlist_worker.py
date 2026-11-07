"""Real worker process limits and cancellation, with no provider or credentials."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.wishlist_interpreter import NativeWorker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hw5-worker-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "python"
        self.worker = NativeWorker(source=self.root, home=self.root / "hermes", python=self.executable)

    def program(self, body):
        self.executable.write_text(f"#!{sys.executable}\n" + body)
        self.executable.chmod(0o700)

    async def test_fixed_process_arguments_clean_environment_and_bounded_frame(self):
        self.program('''import json, os, struct, sys
size = struct.unpack('!I', sys.stdin.buffer.read(4))[0]
request = json.loads(sys.stdin.buffer.read(size))
answer = {'request': request, 'argv': sys.argv[1:], 'env': dict(os.environ),
          'group_is_worker': os.getpgrp() == os.getpid(), 'cwd': os.getcwd()}
raw = json.dumps(answer).encode()
sys.stdout.buffer.write(struct.pack('!I', len(raw)) + raw)
''')
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-do-not-inherit", "HTTPS_PROXY": "synthetic"}):
            result = await self.worker.interpret({"request_id": "synthetic", "text": "test"})
        self.assertEqual(result["request"]["text"], "test")
        self.assertTrue(result["group_is_worker"])
        self.assertEqual(result["cwd"], str(self.root))
        self.assertEqual(result["argv"], ["-B", "-m", "scripts.wishlist_model", "--source", str(self.root),
                                          "--home", str(self.root / "hermes")])
        self.assertNotIn("OPENAI_API_KEY", result["env"])
        self.assertNotIn("HTTPS_PROXY", result["env"])
        self.assertEqual(result["env"]["HERMES_HOME"], str(self.root / "hermes"))
        self.assertEqual(result["env"]["HOME"], str(self.root))

    async def test_timeout_kills_and_reaps_worker(self):
        self.program('''import os, pathlib, time
pathlib.Path('pid').write_text(str(os.getpid()))
time.sleep(60)
''')
        with patch("scripts.wishlist_interpreter.WORKER_TIMEOUT", 0.2):
            with self.assertRaises(TimeoutError):
                await self.worker.interpret({"request_id": "synthetic"})
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_request_cancellation_reaps_worker_before_return(self):
        self.program('''import os, pathlib, time
pathlib.Path('pid').write_text(str(os.getpid()))
time.sleep(60)
''')
        task = asyncio.create_task(self.worker.interpret({"request_id": "synthetic"}))
        async with asyncio.timeout(5):
            while not (self.root / "pid").exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)

    async def test_oversized_output_and_failed_exit_are_rejected(self):
        for body, error in (("import sys\nsys.stdout.buffer.write(b'\\x00\\x01\\x00\\x01')\n", "frame_size"),
                            ("import sys\nsys.stdout.buffer.write(b'\\x00\\x00\\x00\\x02{}')\nsys.exit(1)\n", "language_worker_failed"),
                            ("import json, struct, sys\nr=json.dumps({'x':'a'*9000}).encode()\nsys.stdout.buffer.write(struct.pack('!I',len(r))+r)\n", "language_output_size")):
            with self.subTest(error=error):
                self.program(body)
                with self.assertRaisesRegex(ValueError, error):
                    await self.worker.interpret({"request_id": "synthetic"})


if __name__ == "__main__":
    unittest.main()
