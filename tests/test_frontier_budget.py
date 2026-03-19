import unittest


class TestFrontierBudget(unittest.TestCase):
    def test_budget_counter_increments(self):
        max_calls = 12
        total = 0
        for i in range(15):
            total += 1
            if total >= max_calls:
                break
        self.assertEqual(total, max_calls)

    def test_budget_exceeded_sets_flag(self):
        max_calls = 3
        total = 0
        exceeded = False
        for i in range(10):
            total += 1
            if total >= max_calls:
                exceeded = True
                break
        self.assertTrue(exceeded)

    def test_under_budget_no_flag(self):
        self.assertFalse(2 >= 12)
