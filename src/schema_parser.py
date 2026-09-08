"""
Phase 1 (continued): Opus 5 for schema validation and entity resolution.
This module takes raw extracted text and structures it into validated JSON.
"""
import os
import json
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, validator


class LegData(BaseModel):
    """Individual leg of a parlay bet."""
    sport: str = Field(..., description="Sport type (NFL, NBA, MLB, NHL, etc.)")
    event: str = Field(..., description="Event description (e.g., 'Lakers vs Celtics')")
    player: Optional[str] = Field(None, description="Player name if applicable")
    prop_type: str = Field(..., description="Type of bet (e.g., 'Points Over', 'Moneyline')")
    american_odds: int = Field(..., description="American odds format (e.g., +150, -110)")
    
    @validator('american_odds')
    def validate_odds(cls, v):
        """Validate American odds format."""
        if not isinstance(v, int):
            raise ValueError("Odds must be an integer")
        # American odds should be >= 100 or <= -100 (or exactly 0 in some cases)
        if v != 0 and abs(v) < 100:
            raise ValueError(f"Invalid American odds: {v}")
        return v


class BetData(BaseModel):
    """Structured parlay bet data."""
    sportsbook: str = Field(..., description="Name of the sportsbook")
    date: str = Field(..., description="Date of the bet (YYYY-MM-DD format)")
    total_stake: float = Field(..., description="Total stake amount in USD")
    potential_payout: float = Field(..., description="Potential total payout in USD")
    legs: List[LegData] = Field(..., min_items=1, description="Individual legs of the parlay")
    
    @validator('date')
    def validate_date(cls, v):
        """Validate date format."""
        import re
        if not re.match(r'\d{4}-\d{2}-\d{2}', v):
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v
    
    @validator('total_stake', 'potential_payout')
    def validate_positive(cls, v):
        """Validate positive monetary values."""
        if v < 0:
            raise ValueError("Monetary values must be non-negative")
        return v


class SchemaParser:
    """Parse raw text into structured bet data using Opus 5."""
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize with Opus 5 API key."""
        self.api_key = api_key or os.getenv('ANTHROPIC_API_KEY')
        if not self.api_key:
            raise ValueError("Anthropic API key required. Set ANTHROPIC_API_KEY env var.")
    
    def _get_system_prompt(self) -> str:
        """Return the system prompt for Opus 5 schema parsing."""
        return """You are a data extraction specialist for sports betting slips.
Your task is to extract and structure betting data from raw OCR text.

RULES:
1. Extract ALL legs from the parlay accurately
2. Convert all odds to American format (+/- integer)
3. Identify sport, teams/players, and bet types precisely
4. Standardize US sportsbook terminology (DraftKings, FanDuel, BetMGM, Caesars, etc.)
5. Date must be in YYYY-MM-DD format
6. If information is unclear, mark as "Unknown" rather than guessing

AMERICAN ODDS CONVERSION:
- If you see decimal odds (e.g., 2.50), convert to American: +150
- If you see fractional odds (e.g., 3/2), convert to American: +150
- Keep American odds as-is (e.g., +150, -110)

OUTPUT FORMAT:
Return ONLY valid JSON matching this schema:
{
    "sportsbook": "string",
    "date": "YYYY-MM-DD",
    "total_stake": number,
    "potential_payout": number,
    "legs": [
        {
            "sport": "string",
            "event": "string",
            "player": "string or null",
            "prop_type": "string",
            "american_odds": integer
        }
    ]
}"""

    def parse_to_structured_data(self, raw_text: str) -> BetData:
        """
        Parse raw extracted text into structured bet data.
        
        Args:
            raw_text: Raw text from vision extraction
            
        Returns:
            Validated BetData object
        """
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError("anthropic not installed. Run: pip install anthropic")
        
        client = Anthropic(api_key=self.api_key)
        
        response = client.messages.create(
            model="claude-3-opus-20240229",  # Opus 5 for advanced reasoning
            max_tokens=2000,
            system=self._get_system_prompt(),
            messages=[{
                "role": "user",
                "content": f"""Here is the raw extracted text from a betting slip:

---
{raw_text}
---

Extract and structure this data into valid JSON. Return ONLY the JSON, no other text."""
            }]
        )
        
        # Extract JSON from response
        response_text = response.content[0].text.strip()
        
        # Handle markdown code blocks if present
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        response_text = response_text.strip()
        
        try:
            data = json.loads(response_text)
            return BetData(**data)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse JSON from Opus response: {e}\nRaw response: {response_text}")
        except Exception as e:
            raise ValueError(f"Invalid bet data structure: {e}")
    
    def validate_and_structure(self, raw_text: str) -> Dict[str, Any]:
        """
        Validate and structure raw text, returning as dictionary.
        
        Args:
            raw_text: Raw text from vision extraction
            
        Returns:
            Dictionary of structured bet data
        """
        bet_data = self.parse_to_structured_data(raw_text)
        return bet_data.dict()


def parse_betting_slip(raw_text: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Convenience function to parse raw betting slip text.
    
    Args:
        raw_text: Raw extracted text from vision model
        api_key: Anthropic API key
        
    Returns:
        Dictionary of structured bet data
    """
    parser = SchemaParser(api_key=api_key)
    return parser.validate_and_structure(raw_text)
