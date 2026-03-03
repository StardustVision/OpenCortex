# tests/test_id_generator.py
import unittest
import threading


class TestSnowflakeGenerator(unittest.TestCase):

    def test_generates_positive_int(self):
        from opencortex.utils.id_generator import generate_id
        sid = generate_id()
        self.assertIsInstance(sid, int)
        self.assertGreater(sid, 0)

    def test_uniqueness_single_thread(self):
        from opencortex.utils.id_generator import generate_id
        ids = [generate_id() for _ in range(1000)]
        self.assertEqual(len(set(ids)), 1000)

    def test_monotonically_increasing(self):
        from opencortex.utils.id_generator import generate_id
        ids = [generate_id() for _ in range(100)]
        self.assertEqual(ids, sorted(ids))

    def test_uniqueness_multi_thread(self):
        from opencortex.utils.id_generator import generate_id
        results = []
        def worker():
            for _ in range(500):
                results.append(generate_id())
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(results)), 2000)

    def test_fits_in_64_bits(self):
        from opencortex.utils.id_generator import generate_id
        sid = generate_id()
        self.assertLess(sid, 2**63)


if __name__ == "__main__":
    unittest.main()
