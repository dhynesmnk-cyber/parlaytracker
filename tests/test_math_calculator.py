"""Tests for the parlay math calculator."""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from math_calculator import ParlayCalculator, calculate_parlay_payout


class TestParlayCalculator(unittest.TestCase):
    
    def test_american_to_decimal_positive(self):
        """Test conversion of positive American odds."""
        self.assertEqual(ParlayCalculator.american_to_decimal(+100), 2.0)
        self.assertEqual(ParlayCalculator.american_to_decimal(+150), 2.5)
        self.assertEqual(ParlayCalculator.american_to_decimal(+200), 3.0)
        self.assertEqual(ParlayCalculator.american_to_decimal(+300), 4.0)
    
    def test_american_to_decimal_negative(self):
        """Test conversion of negative American odds."""
        self.assertAlmostEqual(ParlayCalculator.american_to_decimal(-110), 1.909, places=3)
        self.assertAlmostEqual(ParlayCalculator.american_to_decimal(-120), 1.833, places=3)
        self.assertAlmostEqual(ParlayCalculator.american_to_decimal(-150), 1.667, places=3)
        self.assertEqual(ParlayCalculator.american_to_decimal(-200), 1.5)
    
    def test_calculate_parlay_multiplier_two_legs(self):
        """Test parlay multiplier with 2 legs."""
        odds = [+100, -110]
        # 2.0 * 1.909 = 3.818
        multiplier = ParlayCalculator.calculate_parlay_multiplier(odds)
        self.assertAlmostEqual(multiplier, 3.818, places=2)
    
    def test_calculate_parlay_multiplier_three_legs(self):
        """Test parlay multiplier with 3 legs."""
        odds = [-110, -110, -110]
        # 1.909^3 = 6.96
        multiplier = ParlayCalculator.calculate_parlay_multiplier(odds)
        self.assertAlmostEqual(multiplier, 6.96, places=2)
    
    def test_calculate_payout_full(self):
        """Test full payout calculation."""
        stake = 100.0
        odds = [+150, -110]
        
        # +150 = 2.5x, -110 = 1.909x, total = 4.773x
        result = ParlayCalculator.calculate_payout(stake, odds)
        
        self.assertEqual(result['stake'], 100.0)
        self.assertAlmostEqual(result['multiplier'], 4.773, places=2)
        self.assertAlmostEqual(result['total_payout'], 477.27, places=2)
        self.assertAlmostEqual(result['net_profit'], 377.27, places=2)
    
    def test_execute_calculation_script(self):
        """Test that script execution works correctly."""
        result = ParlayCalculator.execute_calculation_script(100.0, [+100, +100])
        
        # +100, +100 = 2.0 * 2.0 = 4.0x multiplier
        self.assertEqual(result['multiplier'], 4.0)
        self.assertEqual(result['total_payout'], 400.0)
        self.assertEqual(result['net_profit'], 300.0)
    
    def test_convenience_function(self):
        """Test the convenience function."""
        result = calculate_parlay_payout(50.0, [-110, -110, -110])
        
        self.assertIn('stake', result)
        self.assertIn('multiplier', result)
        self.assertIn('total_payout', result)
        self.assertIn('net_profit', result)


if __name__ == '__main__':
    unittest.main()
