import unittest
from opencortex.retrieve.entity_index import EntityIndex


class TestEntityIndex(unittest.TestCase):

    def setUp(self):
        self.idx = EntityIndex()

    def test_add_and_lookup(self):
        self.idx.add("col", "m1", ["melanie", "caroline"])
        self.assertEqual(self.idx.get_memories_for_entity("col", "melanie"), {"m1"})
        self.assertEqual(self.idx.get_entities_for_memory("col", "m1"), {"melanie", "caroline"})

    def test_add_normalizes_to_lowercase(self):
        self.idx.add("col", "m1", ["OpenCortex", "REDIS"])
        self.assertEqual(self.idx.get_memories_for_entity("col", "opencortex"), {"m1"})
        self.assertEqual(self.idx.get_memories_for_entity("col", "redis"), {"m1"})

    def test_remove(self):
        self.idx.add("col", "m1", ["melanie"])
        self.idx.add("col", "m2", ["melanie"])
        self.idx.remove("col", "m1")
        self.assertEqual(self.idx.get_memories_for_entity("col", "melanie"), {"m2"})
        self.assertEqual(self.idx.get_entities_for_memory("col", "m1"), set())

    def test_remove_batch(self):
        self.idx.add("col", "m1", ["a"])
        self.idx.add("col", "m2", ["a"])
        self.idx.add("col", "m3", ["a"])
        self.idx.remove_batch("col", ["m1", "m2"])
        self.assertEqual(self.idx.get_memories_for_entity("col", "a"), {"m3"})

    def test_update(self):
        self.idx.add("col", "m1", ["old_entity"])
        self.idx.update("col", "m1", ["new_entity"])
        self.assertEqual(self.idx.get_entities_for_memory("col", "m1"), {"new_entity"})
        self.assertEqual(self.idx.get_memories_for_entity("col", "old_entity"), set())

    def test_per_collection_isolation(self):
        self.idx.add("col_a", "m1", ["entity1"])
        self.idx.add("col_b", "m1", ["entity2"])
        self.assertEqual(self.idx.get_entities_for_memory("col_a", "m1"), {"entity1"})
        self.assertEqual(self.idx.get_entities_for_memory("col_b", "m1"), {"entity2"})

    def test_empty_collection(self):
        self.assertEqual(self.idx.get_memories_for_entity("nonexist", "x"), set())
        self.assertEqual(self.idx.get_entities_for_memory("nonexist", "y"), set())

    def test_entity_degree(self):
        for i in range(100):
            self.idx.add("col", f"m{i}", ["popular"])
        self.assertEqual(len(self.idx.get_memories_for_entity("col", "popular")), 100)

    def test_is_ready(self):
        self.assertFalse(self.idx.is_ready("col"))
        self.idx.add("col", "m1", ["a"])
        self.assertTrue(self.idx.is_ready("col"))


if __name__ == "__main__":
    unittest.main()
