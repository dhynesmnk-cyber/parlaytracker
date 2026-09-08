# ParlayTracker

A sports betting parlay verification system that uses AI to extract, structure, verify, and analyze betting slips.

## Architecture Overview

This system follows a cost-optimized, multi-phase approach:

### Phase 1: Model Routing and Cost Optimization
- **Vision Extraction (Claude Haiku)**: Low-cost OCR to extract raw text from betting slip screenshots
- **Schema Parsing (Opus 5)**: Advanced reasoning to structure and validate extracted data into JSON

### Phase 2: Data Layer and US Market Standardization
- **PostgreSQL Database**: Three-table schema (Bets, Legs, Context)
- **American Odds Support**: Full support for +150, -110 format
- **US Sportsbook Terminology**: Standardized handling of DraftKings, FanDuel, BetMGM, etc.

### Phase 3: Agentic Verification and Strict Mathematics
- **Web Search (Tavily API)**: Opus 5 formulates precise queries to verify each leg
- **Python Code Interpreter**: All mathematical calculations are done by Python scripts, NOT the model (prevents hallucination)

### Phase 4: Nuanced Synthesis
- **Analytical Summaries**: Opus 5 generates readable breakdowns with source citations
- **Financial Summary**: Displayed exactly as calculated by Python
- **Causal Analysis**: Clear explanations of why each leg won/lost

## Project Structure

```
/workspace
├── src/
│   ├── __init__.py           # Package init
│   ├── models.py             # SQLAlchemy database models
│   ├── database.py           # Database connection management
│   ├── vision_extractor.py   # Phase 1: Haiku/GCV OCR extraction
│   ├── schema_parser.py      # Phase 1: Opus 5 schema validation
│   ├── math_calculator.py    # Phase 3: Python code interpreter
│   ├── search_agent.py       # Phase 3: Tavily web search
│   ├── verification_engine.py # Phase 3&4: Opus 5 verification & synthesis
│   └── cli.py                # Command-line interface
├── tests/
│   ├── __init__.py
│   └── test_math_calculator.py
├── mock_screenshots/         # Test images
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable template
└── README.md                 # This file
```

## Installation

### Prerequisites
- Python 3.9+
- Anthropic API key (for Haiku and Opus 5)
- Tavily API key (for web search)
- (Optional) Google Cloud Vision credentials

### Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

3. **Initialize the database:**
   ```bash
   cd src
   python cli.py init-db
   ```

## Usage

### CLI Commands

```bash
# Initialize database
python src/cli.py init-db

# Test math calculator
python src/cli.py test-math

# Process a mock betting slip
python src/cli.py mock-process --store

# Show database schema
python src/cli.py show-schema

# Test leg verification (requires API keys)
python src/cli.py verify-leg
```

### Programmatic Usage

```python
from src.verification_engine import process_betting_slip

result = process_betting_slip(
    image_path="path/to/betting_slip.png",
    vision_api_key="your_anthropic_key",
    anthropic_api_key="your_anthropic_key",
    tavily_api_key="your_tavily_key"
)

print(f"Bet ID: {result['bet_id']}")
print(f"Net Profit/Loss: ${result['calculation']['net_profit']:.2f}")
print(result['summary'])
```

## Database Schema

### bets
- `id`: Primary key
- `date`: Bet date
- `sportsbook`: Sportsbook name
- `total_stake`: Stake amount in USD
- `final_status`: pending/won/lost/partial
- `net_profit_loss`: Calculated P/L

### legs
- `id`: Primary key
- `bet_id`: Foreign key to bets
- `sport`: NFL, NBA, MLB, NHL, etc.
- `event`: Game description
- `player`: Player name (nullable)
- `prop_type`: Bet type
- `american_odds`: Odds in American format
- `leg_status`: pending/won/lost/void

### context
- `id`: Primary key
- `leg_id`: Foreign key to legs (unique)
- `bet_id`: Foreign key to bets
- `search_query`: Query used for verification
- `raw_search_results`: JSON search results
- `verified_outcome`: won/lost/void/unknown
- `source_citations`: JSON list of sources
- `verification_notes`: Analysis notes

## Testing

```bash
# Run unit tests
pytest tests/

# Run math calculator tests specifically
python -m pytest tests/test_math_calculator.py -v
```

## Critical Build Notes

1. **No GUI until backend is verified**: The CLI must produce 100% accurate data before any visual layer is built.

2. **Opus 5 usage is restricted**: 
   - ✅ Schema validation and entity resolution
   - ✅ Web search query formulation
   - ✅ Verification analysis
   - ✅ Analytical summary generation
   - ❌ Mathematical calculations (must use Python)

3. **Model routing is critical**:
   - Haiku for OCR (fast, cheap)
   - Opus 5 for reasoning (expensive, justified)

## Required API Keys

| Service | Purpose | Get Key |
|---------|---------|---------|
| Anthropic | Haiku OCR + Opus 5 reasoning | https://console.anthropic.com |
| Tavily | Web search verification | https://tavily.ai |
| Google Cloud (optional) | Alternative OCR | https://cloud.google.com/vision |

## License

MIT
