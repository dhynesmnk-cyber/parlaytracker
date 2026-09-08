# ParlayTracker Frontend

A web-based UI for testing the ParlayTracker backend pipeline.

## Quick Start

### Run the Frontend

```bash
cd /workspace
streamlit run frontend.py
```

The app will open in your browser at `http://localhost:8501`

### Features

1. **Process Betting Slip**
   - Manual Entry: Input bet details leg by leg
   - Mock Data: Load pre-defined test data
   - Automatic payout calculation using American odds
   - Save bets to database

2. **View Database**
   - See all stored bets
   - View bet details including legs and odds
   - Check verification status and notes

3. **Verify Leg** (requires API keys)
   - Select pending legs from database
   - Run web search verification via Tavily
   - Store search results and analysis

4. **Settings**
   - View API key status
   - Check database statistics

## API Keys Required

For full functionality, set these in your `.env` file:

```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
TAVILY_API_KEY=tvly-dev-...
```

## Backend CLI Alternative

You can also test via command line:

```bash
# Test math calculator
python src/cli.py test-math

# Process mock betting slip
python src/cli.py mock-process --store

# View database schema
python src/cli.py show-schema

# Test leg verification (requires API keys)
python src/cli.py verify-leg
```

## Architecture

- **Frontend**: Streamlit web UI (`frontend.py`)
- **Backend**: Python modules in `src/`
- **Database**: SQLite (default) or PostgreSQL
- **APIs**: Anthropic (Claude), Tavily (Search)

## Deployment Note

This is a Python application requiring:
- Python runtime environment
- Database (SQLite/PostgreSQL)
- API access

For production deployment, use platforms like:
- Railway.app
- Render.com
- Fly.io
- Heroku

Netlify only supports static sites and serverless functions, not full Python applications with databases.
