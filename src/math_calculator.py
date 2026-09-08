"""
Phase 3: Python Code Interpreter for strict mathematics.
Opus 5 is forbidden from calculating payouts directly - it must use this tool.
"""
import subprocess
import json
from typing import Dict, Any, List


class ParlayCalculator:
    """
    Calculate parlay payouts using Python code execution.
    This ensures mathematical accuracy and prevents model hallucination.
    """
    
    @staticmethod
    def american_to_decimal(american_odds: int) -> float:
        """Convert American odds to decimal odds."""
        if american_odds > 0:
            return (american_odds / 100) + 1
        else:
            return (100 / abs(american_odds)) + 1
    
    @staticmethod
    def calculate_parlay_multiplier(legs_odds: List[int]) -> float:
        """
        Calculate the total parlay multiplier from a list of American odds.
        
        Args:
            legs_odds: List of American odds for each leg
            
        Returns:
            Total multiplier (e.g., 5.5 means 5.5x return)
        """
        multiplier = 1.0
        for odds in legs_odds:
            decimal_odds = ParlayCalculator.american_to_decimal(odds)
            multiplier *= decimal_odds
        return multiplier
    
    @staticmethod
    def calculate_payout(stake: float, legs_odds: List[int]) -> Dict[str, float]:
        """
        Calculate full parlay payout details.
        
        Args:
            stake: Total stake amount in USD
            legs_odds: List of American odds for each leg
            
        Returns:
            Dictionary with payout breakdown
        """
        multiplier = ParlayCalculator.calculate_parlay_multiplier(legs_odds)
        total_payout = stake * multiplier
        net_profit = total_payout - stake
        
        return {
            "stake": stake,
            "multiplier": round(multiplier, 4),
            "total_payout": round(total_payout, 2),
            "net_profit": round(net_profit, 2)
        }
    
    @staticmethod
    def execute_calculation_script(stake: float, legs_odds: List[int]) -> Dict[str, float]:
        """
        Execute a Python script to calculate parlay math.
        This is the method Opus 5 should call via code interpreter.
        
        Args:
            stake: Total stake amount
            legs_odds: List of American odds
            
        Returns:
            Calculation results
        """
        # Generate the Python script
        script = f'''
import json

def american_to_decimal(odds):
    if odds > 0:
        return (odds / 100) + 1
    else:
        return (100 / abs(odds)) + 1

def calculate_parlay(stake, odds_list):
    multiplier = 1.0
    for odds in odds_list:
        multiplier *= american_to_decimal(odds)
    total_payout = stake * multiplier
    net_profit = total_payout - stake
    return {{
        "stake": stake,
        "multiplier": round(multiplier, 4),
        "total_payout": round(total_payout, 2),
        "net_profit": round(net_profit, 2)
    }}

result = calculate_parlay({stake}, {legs_odds})
print(json.dumps(result))
'''
        
        # Execute the script
        result = subprocess.run(
            ['python3', '-c', script],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"Calculation script failed: {result.stderr}")
        
        return json.loads(result.stdout.strip())


def calculate_parlay_payout(stake: float, legs_odds: List[int]) -> Dict[str, float]:
    """
    Convenience function to calculate parlay payout.
    
    Args:
        stake: Total stake amount in USD
        legs_odds: List of American odds for each leg
        
    Returns:
        Dictionary with payout details
    """
    return ParlayCalculator.execute_calculation_script(stake, legs_odds)
