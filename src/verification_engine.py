"""
Phase 3 & 4: Opus 5 as investigative researcher with strict math and synthesis.
This module orchestrates verification and generates analytical summaries.
"""
import os
import json
from datetime import datetime
from typing import Optional, Dict, Any, List

from .database import get_session, init_db
from .models import Bet, Leg, Context
from .search_agent import SearchAgent
from .math_calculator import ParlayCalculator


class VerificationEngine:
    """
    Opus 5-powered verification engine.
    Acts as investigative researcher to verify bet outcomes.
    """
    
    def __init__(self, anthropic_api_key: Optional[str] = None, 
                 tavily_api_key: Optional[str] = None):
        """
        Initialize the verification engine.
        
        Args:
            anthropic_api_key: Anthropic API key for Opus 5
            tavily_api_key: Tavily API key for web search
        """
        self.anthropic_api_key = anthropic_api_key or os.getenv('ANTHROPIC_API_KEY')
        self.tavily_api_key = tavily_api_key or os.getenv('TAVILY_API_KEY')
        
        if not self.anthropic_api_key:
            raise ValueError("Anthropic API key required")
        
        self.search_agent = SearchAgent(api_key=self.tavily_api_key)
        self._init_anthropic()
    
    def _init_anthropic(self):
        """Initialize Anthropic client."""
        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=self.anthropic_api_key)
        except ImportError:
            raise ImportError("anthropic not installed")
    
    def _get_verification_prompt(self, leg: Dict[str, Any], 
                                  search_results: Dict[str, Any]) -> str:
        """Generate prompt for Opus 5 to analyze search results."""
        return f"""You are an investigative researcher verifying sports betting outcomes.

Your task: Analyze the search results and determine if this bet leg WON, LOST, or is VOID/UNKNOWN.

BET LEG DETAILS:
- Sport: {leg.get('sport', 'Unknown')}
- Event: {leg.get('event', 'Unknown')}
- Player: {leg.get('player', 'N/A')}
- Prop Type: {leg.get('prop_type', 'Unknown')}
- Odds: {leg.get('american_odds', 'Unknown')}

SEARCH RESULTS:
{json.dumps(search_results.get('results', [])[:3], indent=2)}

INSTRUCTIONS:
1. Carefully read all search results
2. Find the specific game statistics or outcome
3. Compare against the prop type to determine win/loss
4. If you cannot find definitive information, mark as UNKNOWN
5. Cite your sources explicitly (URL and publication)

Respond in this EXACT JSON format:
{{
    "outcome": "won|lost|void|unknown",
    "actual_stat": "The actual statistic or score achieved",
    "required_stat": "What was needed to win (from prop_type)",
    "reasoning": "Clear explanation of why this outcome occurred",
    "sources": [
        {{"title": "Source title", "url": "https://..."}}
    ]
}}"""

    def verify_single_leg(self, leg: Dict[str, Any], 
                          game_date: str) -> Dict[str, Any]:
        """
        Verify a single leg using web search and Opus 5 analysis.
        
        Args:
            leg: Leg data dictionary
            game_date: Date of the game
            
        Returns:
            Verification result dictionary
        """
        # Step 1: Search for relevant information
        search_results = self.search_agent.search_leg_outcome(leg, game_date)
        
        # Step 2: Use Opus 5 to analyze results
        prompt = self._get_verification_prompt(leg, search_results)
        
        response = self.client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1500,
            system="You are a precise sports betting verification assistant. Always respond with valid JSON.",
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Parse the response
        response_text = response.content[0].text.strip()
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        response_text = response_text.strip()
        
        try:
            verification_result = json.loads(response_text)
        except json.JSONDecodeError:
            verification_result = {
                "outcome": "unknown",
                "actual_stat": "Could not parse",
                "required_stat": "N/A",
                "reasoning": "Failed to parse verification response",
                "sources": []
            }
        
        # Add search metadata
        verification_result["search_query"] = search_results.get("query", "")
        verification_result["raw_search_results"] = search_results.get("results", [])
        
        return verification_result
    
    def calculate_and_store_math(self, bet_id: int, legs: List[Dict[str, Any]], 
                                  stake: float, session=None) -> Dict[str, float]:
        """
        Calculate parlay math using Python code interpreter.
        Store results in database.
        
        IMPORTANT: Opus 5 is FORBIDDEN from calculating this directly.
        This method MUST be used for all mathematical operations.
        
        Args:
            bet_id: Database ID of the bet
            legs: List of leg dictionaries with american_odds
            stake: Total stake amount
            session: Database session
            
        Returns:
            Calculation results
        """
        # Extract odds from legs
        odds_list = [leg['american_odds'] for leg in legs]
        
        # Calculate using Python code interpreter (NOT the model!)
        calculation = ParlayCalculator.execute_calculation_script(stake, odds_list)
        
        # Store in database
        if session is None:
            session = get_session()
            should_commit = True
        else:
            should_commit = False
        
        try:
            bet = session.query(Bet).filter(Bet.id == bet_id).first()
            if bet:
                bet.net_profit_loss = calculation['net_profit']
                if should_commit:
                    session.commit()
        except Exception as e:
            if should_commit:
                session.rollback()
            raise e
        finally:
            if should_commit:
                session.close()
        
        return calculation
    
    def generate_analytical_summary(self, bet: Bet, 
                                     legs: List[Leg],
                                     contexts: List[Context]) -> str:
        """
        Generate nuanced analytical summary using Opus 5.
        Must cite sources from Context table.
        
        Args:
            bet: Bet object
            legs: List of Leg objects
            contexts: List of Context objects with verification data
            
        Returns:
            Analytical summary text
        """
        # Prepare context data
        leg_contexts = []
        for leg, ctx in zip(legs, contexts):
            leg_contexts.append({
                "leg_id": leg.id,
                "sport": leg.sport,
                "event": leg.event,
                "player": leg.player,
                "prop_type": leg.prop_type,
                "odds": leg.american_odds,
                "status": leg.leg_status,
                "verified_outcome": ctx.verified_outcome if ctx else "unknown",
                "verification_notes": ctx.verification_notes if ctx else "",
                "sources": ctx.source_citations if ctx else []
            })
        
        # Get calculated financial data
        odds_list = [leg.american_odds for leg in legs]
        calculation = ParlayCalculator.execute_calculation_script(bet.total_stake, odds_list)
        
        prompt = f"""You are generating an analytical summary of a parlay bet.

FINANCIAL SUMMARY (CALCULATED BY PYTHON - DO NOT RECALCULATE):
- Sportsbook: {bet.sportsbook}
- Date: {bet.date}
- Total Stake: ${bet.total_stake:.2f}
- Net Profit/Loss: ${calculation['net_profit']:.2f}
- Multiplier: {calculation['multiplier']}x

PARLAY LEGS AND OUTCOMES:
{json.dumps(leg_contexts, indent=2)}

YOUR TASK:
Write a clear, analytical breakdown explaining WHY each leg won or lost.

CRITICAL RULES:
1. For FINANCIAL SUMMARY: Display the exact net profit/loss provided above. DO NOT recalculate.
2. For ANALYTICAL BREAKDOWN: State the causal link clearly for each leg.
   Example: "Leg 3 failed because the starting quarterback was ruled out at halftime due to a hamstring strain, as reported by ESPN at 8:15 PM EST."
3. CITE YOUR SOURCES: Every claim must reference a source from the context data.
4. Be specific about dates, times, and sources.

Structure your response:

## Financial Summary
[Display the calculated numbers]

## Analytical Breakdown
[Explain each leg's outcome with causal links and citations]"""

        response = self.client.messages.create(
            model="claude-3-opus-20240229",
            max_tokens=2000,
            system="You are a sports betting analyst. Be precise, cite sources, and never recalculate financial data.",
            messages=[{"role": "user", "content": prompt}]
        )
        
        return response.content[0].text


def process_betting_slip(image_path: str, 
                         vision_api_key: Optional[str] = None,
                         anthropic_api_key: Optional[str] = None,
                         tavily_api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Complete pipeline: extract -> structure -> verify -> calculate -> summarize.
    
    Args:
        image_path: Path to betting slip screenshot
        vision_api_key: API key for vision extraction
        anthropic_api_key: API key for Opus 5
        tavily_api_key: API key for Tavily search
        
    Returns:
        Complete processing results
    """
    from .vision_extractor import VisionExtractor
    from .schema_parser import SchemaParser
    
    # Phase 1: Extract text (Haiku - low cost)
    vision_extractor = VisionExtractor(api_key=vision_api_key, provider="haiku")
    raw_text = vision_extractor.extract_text(image_path)
    
    # Phase 1: Structure data (Opus 5 - advanced reasoning)
    schema_parser = SchemaParser(api_key=anthropic_api_key)
    structured_data = schema_parser.validate_and_structure(raw_text)
    
    # Initialize database
    engine = init_db()
    session = get_session(engine)
    
    try:
        # Phase 2: Store in database
        bet = Bet(
            date=datetime.strptime(structured_data['date'], '%Y-%m-%d'),
            sportsbook=structured_data['sportsbook'],
            total_stake=structured_data['total_stake']
        )
        session.add(bet)
        session.flush()
        
        legs_objects = []
        for leg_data in structured_data['legs']:
            leg = Leg(
                bet_id=bet.id,
                sport=leg_data['sport'],
                event=leg_data['event'],
                player=leg_data.get('player'),
                prop_type=leg_data['prop_type'],
                american_odds=leg_data['american_odds']
            )
            session.add(leg)
            legs_objects.append(leg)
        
        session.flush()
        
        # Phase 3: Verify each leg
        verification_engine = VerificationEngine(
            anthropic_api_key=anthropic_api_key,
            tavily_api_key=tavily_api_key
        )
        
        contexts_objects = []
        for leg, leg_data in zip(legs_objects, structured_data['legs']):
            verification = verification_engine.verify_single_leg(
                leg_data, 
                structured_data['date']
            )
            
            context = Context(
                leg_id=leg.id,
                bet_id=bet.id,
                search_query=verification.get('search_query', ''),
                raw_search_results=verification.get('raw_search_results', []),
                verified_outcome=verification.get('outcome', 'unknown'),
                source_citations=verification.get('sources', []),
                verification_notes=verification.get('reasoning', '')
            )
            session.add(context)
            contexts_objects.append(context)
            
            # Update leg status
            leg.leg_status = verification.get('outcome', 'pending')
        
        # Phase 3: Calculate math with Python
        calculation = verification_engine.calculate_and_store_math(
            bet.id, 
            structured_data['legs'],
            structured_data['total_stake'],
            session
        )
        
        session.commit()
        
        # Phase 4: Generate analytical summary
        summary = verification_engine.generate_analytical_summary(
            bet, legs_objects, contexts_objects
        )
        
        return {
            "bet_id": bet.id,
            "structured_data": structured_data,
            "calculation": calculation,
            "summary": summary,
            "legs": [l.id for l in legs_objects],
            "contexts": [c.id for c in contexts_objects]
        }
        
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()
