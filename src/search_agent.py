"""
Phase 3: Web Search API integration for agentic verification.
Uses Tavily to search for match reports, injury updates, and game outcomes.
"""
import os
from typing import Optional, Dict, Any, List


class SearchAgent:
    """
    Investigative researcher agent using Tavily web search.
    Formulates precise queries to verify bet outcomes.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the search agent.
        
        Args:
            api_key: Tavily API key
        """
        self.api_key = api_key or os.getenv('TAVILY_API_KEY')
        if not self.api_key:
            raise ValueError("Tavily API key required. Set TAVILY_API_KEY env var.")
        
        self._init_client()
    
    def _init_client(self):
        """Initialize Tavily client."""
        try:
            from tavily import TavilyClient
            self.client = TavilyClient(api_key=self.api_key)
        except ImportError:
            raise ImportError("tavily-python not installed. Run: pip install tavily-python")
    
    def formulate_query(self, leg: Dict[str, Any], outcome_hint: str = "") -> str:
        """
        Formulate a precise search query for verifying a bet leg.
        
        Args:
            leg: Leg data dictionary with sport, event, player, prop_type
            outcome_hint: Optional hint about what to look for (e.g., "injury", "score")
            
        Returns:
            Optimized search query string
        """
        sport = leg.get('sport', '')
        event = leg.get('event', '')
        player = leg.get('player')
        prop_type = leg.get('prop_type', '')
        
        # Build contextual query
        query_parts = []
        
        if player:
            query_parts.append(f"{player}")
        
        if sport:
            query_parts.append(sport)
        
        if event:
            query_parts.append(event)
        
        if prop_type:
            query_parts.append(prop_type)
        
        if outcome_hint:
            query_parts.append(outcome_hint)
        
        return " ".join(query_parts)
    
    def search_leg_outcome(self, leg: Dict[str, Any], 
                           game_date: str,
                           include_stats: bool = True) -> Dict[str, Any]:
        """
        Search for verification information about a specific leg.
        
        Args:
            leg: Leg data dictionary
            game_date: Date of the game/event
            include_stats: Whether to include detailed stats in search
            
        Returns:
            Search results with context
        """
        # Formulate primary query
        query = self.formulate_query(leg)
        query += f" {game_date}"
        
        # Execute search
        results = self.client.search(
            query=query,
            search_depth="advanced",
            max_results=5
        )
        
        # Extract relevant information
        search_summary = {
            "query": query,
            "results": [],
            "raw_response": results
        }
        
        for result in results.get('results', []):
            search_summary["results"].append({
                "title": result.get('title', ''),
                "url": result.get('url', ''),
                "content": result.get('content', ''),
                "score": result.get('score', 0)
            })
        
        return search_summary
    
    def verify_player_prop(self, player: str, prop_type: str, 
                          event: str, date: str) -> Dict[str, Any]:
        """
        Specifically verify a player prop bet outcome.
        
        Args:
            player: Player name
            prop_type: Type of prop (e.g., "Points Over 25.5")
            event: Event/game description
            date: Game date
            
        Returns:
            Verification results
        """
        # Search for player stats in the specific game
        query = f"{player} stats {event} {date}"
        
        results = self.client.search(
            query=query,
            search_depth="advanced",
            max_results=5
        )
        
        return {
            "query": query,
            "player": player,
            "prop_type": prop_type,
            "results": results.get('results', []),
            "raw_response": results
        }
    
    def search_injury_reports(self, player: str, team: str, 
                              date: str) -> Dict[str, Any]:
        """
        Search for injury reports affecting a player.
        
        Args:
            player: Player name
            team: Team name
            date: Date range
            
        Returns:
            Injury report findings
        """
        query = f"{player} injury report {team} {date}"
        
        results = self.client.search(
            query=query,
            search_depth="advanced",
            max_results=5
        )
        
        return {
            "query": query,
            "player": player,
            "team": team,
            "results": results.get('results', []),
            "raw_response": results
        }


def search_leg_verification(leg: Dict[str, Any], game_date: str, 
                            api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Convenience function to search for leg verification.
    
    Args:
        leg: Leg data dictionary
        game_date: Date of the game
        api_key: Tavily API key
        
    Returns:
        Search results for verification
    """
    agent = SearchAgent(api_key=api_key)
    return agent.search_leg_outcome(leg, game_date)
