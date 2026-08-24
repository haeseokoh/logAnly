import io
import unittest

import bugreport_index as b


class ChunkTests(unittest.TestCase):
    def test_section_log_stack_and_metadata(self):
        raw = """------ SYSTEM LOG (logcat -d) ------
--------- beginning of main
03-26 22:44:01.123  100  101 E TestTag: failed
FATAL EXCEPTION: main
Process: com.example, PID: 100
java.lang.IllegalStateException: bad
    at com.example.Main.run(Main.java:1)
------ MEMORY INFO (/proc/meminfo) ------
MemTotal: 1 kB
""".encode().splitlines(keepends=True)
        chunks = list(b.iter_chunks(raw, 50))
        self.assertTrue(any(c.section.startswith("SYSTEM LOG") for c in chunks))
        stack = next(c for c in chunks if c.kind == "stack")
        meta = b.metadata(stack)
        self.assertEqual(meta["is_stack"], 1)
        self.assertIn("com.example", meta["process"])
        self.assertIn("100", meta["pid"])

    def test_decode_replacement(self):
        self.assertIn("\ufffd", b.decode_line(b"x\xff\n"))

    def test_ollama_default_is_explicit_and_installed(self):
        self.assertEqual(b.choose_model(["qwen3:8b", "gemma4:e4b"], None), "gemma4:e4b")
        with self.assertRaises(RuntimeError):
            b.choose_model(["qwen3:8b"], None)

    def test_requested_model_must_be_installed(self):
        self.assertEqual(b.choose_model(["gemma4:e4b", "qwen3:8b"], "qwen3:8b"), "qwen3:8b")
        with self.assertRaises(RuntimeError):
            b.choose_model(["gemma4:e4b"], "missing:latest")

    def test_embedding_model_alias_and_normalized_blob(self):
        self.assertEqual(b.resolve_embedding_model(["embeddinggemma:latest"], "embeddinggemma"),
                         "embeddinggemma:latest")
        blob, norm = b.normalized_blob([3.0, 4.0])
        values = b.blob_vector(blob)
        self.assertAlmostEqual(norm, 5.0)
        self.assertAlmostEqual(sum(value * value for value in values), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
