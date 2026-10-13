import unittest
import main


class T(unittest.TestCase):
    def test_even(self):
        self.assertTrue(main.is_even(12))

    def test_odd(self):
        self.assertFalse(main.is_even(7))
