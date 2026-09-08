#!/usr/bin/env python3
"""
Command Line Interface for ParlayTracker.
Test the backend pipeline without a GUI.
"""
import os
import sys
import argparse
import json
from datetime import datetime
from typing import Optional

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_session
from models import Bet, Leg, Context
from math_calculator import calculate_parlay_payout


def cmd_init_db(args):
    """Initialize the database."""
    print("Initializing database...")
    engine = init_db()
    print(f"Database initialized at: {engine.url}")
    
    # Create tables
    from models import Base
    Base.metadata.create_all(engine)
    print("Tables created successfully.")


def cmd_test_math(args):
    """Test the parlay math calculator."""
    print("\n=== Testing Parlay Math Calculator ===\n")
    
    # Test case 1: Simple 2-leg parlay
    stake = 100.0
    odds = [+150, -110]
    
    print(f"Test 1: ${stake} stake on {odds}")
    result = calculate_parlay_payout(stake, odds)
    print(f"  Multiplier: {result['multiplier']}x")
    print(f"  Total Payout: ${result['total_payout']:.2f}")
    print(f"  Net Profit: ${result['net_profit']:.2f}")
    
    # Test case 2: 4-leg parlay with mixed odds
    stake = 50.0
    odds = [-120, +130, -105, +200]
    
    print(f"\nTest 2: ${stake} stake on {odds}")
    result = calculate_parlay_payout(stake, odds)
    print(f"  Multiplier: {result['multiplier']}x")
    print(f"  Total Payout: ${result['total_payout']:.2f}")
    print(f"  Net Profit: ${result['net_profit']:.2f}")
    
    # Test case 3: Long shot parlay
    stake = 25.0
    odds = [+300, +250, +400]
    
    print(f"\nTest 3: ${stake} stake on {odds}")
    result = calculate_parlay_payout(stake, odds)
    print(f"  Multiplier: {result['multiplier']}x")
    print(f"  Total Payout: ${result['total_payout']:.2f}")
    print(f"  Net Profit: ${result['net_profit']:.2f}")
    
    print("\n✓ Math calculator tests passed!")


def cmd_mock_process(args):
    """Process a mock betting slip (simulated)."""
    print("\n=== Processing Mock Betting Slip ===\n")
    
    # Simulate structured data that would come from vision + schema parsing
    mock_data = {
        "sportsbook": "DraftKings",
        "date": "2024-01-15",
        "total_stake": 100.0,
        "potential_payout": 572.50,
        "legs": [
            {
                "sport": "NFL",
                "event": "Kansas City Chiefs @ Buffalo Bills",
                "player": "Patrick Mahomes",
                "prop_type": "Passing Yards Over 275.5",
                "american_odds": -110
            },
            {
                "sport": "NBA",
                "event": "Los Angeles Lakers vs Boston Celtics",
                "player": "LeBron James",
                "prop_type": "Points Over 25.5",
                "american_odds": -115
            },
            {
                "sport": "NFL",
                "event": "San Francisco 49ers @ Green Bay Packers",
                "player": "Christian McCaffrey",
                "prop_type": "Anytime Touchdown Scorer",
                "american_odds": -125
            }
        ]
    }
    
    print("Structured Data:")
    print(json.dumps(mock_data, indent=2))
    
    # Calculate math
    odds_list = [leg['american_odds'] for leg in mock_data['legs']]
    calculation = calculate_parlay_payout(mock_data['total_stake'], odds_list)
    
    print("\nCalculated Payout (Python):")
    print(f"  Multiplier: {calculation['multiplier']}x")
    print(f"  Total Payout: ${calculation['total_payout']:.2f}")
    print(f"  Net Profit: ${calculation['net_profit']:.2f}")
    
    # Store in database
    if args.store:
        engine = init_db()
        session = get_session(engine)
        
        try:
            bet = Bet(
                date=datetime.strptime(mock_data['date'], '%Y-%m-%d'),
                sportsbook=mock_data['sportsbook'],
                total_stake=mock_data['total_stake'],
                net_profit_loss=calculation['net_profit']
            )
            session.add(bet)
            session.flush()
            
            for leg_data in mock_data['legs']:
                leg = Leg(
                    bet_id=bet.id,
                    sport=leg_data['sport'],
                    event=leg_data['event'],
                    player=leg_data.get('player'),
                    prop_type=leg_data['prop_type'],
                    american_odds=leg_data['american_odds']
                )
                session.add(leg)
            
            session.commit()
            print(f"\n✓ Stored in database with bet_id={bet.id}")
            
        except Exception as e:
            session.rollback()
            print(f"\n✗ Database error: {e}")
        finally:
            session.close()
    
    print("\n✓ Mock processing complete!")


def cmd_verify_leg(args):
    """Test verification of a single leg."""
    print("\n=== Testing Leg Verification ===\n")
    
    if not os.getenv('TAVILY_API_KEY'):
        print("⚠ TAVILY_API_KEY not set. Skipping live search test.")
        print("Set TAVILY_API_KEY environment variable to test verification.")
        return
    
    if not os.getenv('ANTHROPIC_API_KEY'):
        print("⚠ ANTHROPIC_API_KEY not set. Skipping Opus 5 analysis test.")
        print("Set ANTHROPIC_API_KEY environment variable to test verification.")
        return
    
    from src.search_agent import SearchAgent
    from src.verification_engine import VerificationEngine
    
    # Test leg
    test_leg = {
        "sport": "NFL",
        "event": "Chiefs vs Bills",
        "player": "Patrick Mahomes",
        "prop_type": "Passing Yards Over 275.5",
        "american_odds": -110
    }
    
    game_date = "2024-01-15"
    
    print(f"Verifying: {test_leg['player']} - {test_leg['prop_type']}")
    print(f"Game Date: {game_date}")
    print()
    
    # Initialize verification engine
    engine = VerificationEngine()
    
    # Search for information
    print("Searching for game information...")
    search_results = engine.search_agent.search_leg_outcome(test_leg, game_date)
    print(f"Search Query: {search_results['query']}")
    print(f"Results Found: {len(search_results['results'])}")
    
    for i, result in enumerate(search_results['results'][:3], 1):
        print(f"\n{i}. {result.get('title', 'No title')}")
        print(f"   URL: {result.get('url', 'No URL')}")
        print(f"   Content: {result.get('content', '')[:200]}...")
    
    print("\n✓ Verification search test complete!")


def cmd_show_schema(args):
    """Display the database schema."""
    print("\n=== Database Schema ===\n")
    
    print("""
TABLE: bets
---------
- id (Integer, Primary Key)
- date (DateTime)
- sportsbook (String)
- total_stake (Float)
- final_status (String): pending, won, lost, partial
- net_profit_loss (Float)
- created_at (DateTime)
- updated_at (DateTime)

TABLE: legs
-----------
- id (Integer, Primary Key)
- bet_id (Integer, Foreign Key -> bets.id)
- sport (String): NFL, NBA, MLB, NHL, etc.
- event (String): Game description
- player (String, nullable): Player name
- prop_type (String): Type of bet
- american_odds (Integer): e.g., +150, -110
- leg_status (String): pending, won, lost, void
- created_at (DateTime)

TABLE: context
--------------
- id (Integer, Primary Key)
- leg_id (Integer, Foreign Key -> legs.id, unique)
- bet_id (Integer, Foreign Key -> bets.id)
- search_query (Text): Query used for verification
- raw_search_results (JSON): Raw search API response
- verified_outcome (String): won, lost, void, unknown
- source_citations (JSON): List of cited sources
- verification_notes (Text): Analysis notes
- created_at (DateTime)
- updated_at (DateTime)
""")


def main():
    parser = argparse.ArgumentParser(
        description="ParlayTracker CLI - Betting slip verification system"
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # init-db command
    p_init = subparsers.add_parser('init-db', help='Initialize the database')
    p_init.set_defaults(func=cmd_init_db)
    
    # test-math command
    p_math = subparsers.add_parser('test-math', help='Test parlay math calculator')
    p_math.set_defaults(func=cmd_test_math)
    
    # mock-process command
    p_mock = subparsers.add_parser('mock-process', 
                                    help='Process a mock betting slip')
    p_mock.add_argument('--store', action='store_true', 
                        help='Store results in database')
    p_mock.set_defaults(func=cmd_mock_process)
    
    # verify-leg command
    p_verify = subparsers.add_parser('verify-leg', 
                                      help='Test leg verification')
    p_verify.set_defaults(func=cmd_verify_leg)
    
    # show-schema command
    p_schema = subparsers.add_parser('show-schema', 
                                      help='Display database schema')
    p_schema.set_defaults(func=cmd_show_schema)
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    
    args.func(args)


if __name__ == '__main__':
    main()
