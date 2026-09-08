# ParlayTracker - Testing Guide

## ✅ Frontend is Running!

The Streamlit web application is now accessible at:
- **Local URL**: http://localhost:8501
- **Network URL**: http://21.0.2.68:8501
- **External URL**: http://47.236.31.202:8501

## How to Test

### Option 1: Web Interface (Recommended)

Open your browser and go to `http://localhost:8501`

**Steps:**
1. Click "Process Betting Slip" in the sidebar
2. Try "Use Mock Data" tab → Click "Load Mock Data"
3. Review the calculated payout
4. Click "Save Mock Data to Database"
5. Go to "View Database" to see your saved bet
6. Check "Settings" to verify API keys are configured

**Features Available:**
- ✅ Manual bet entry with American odds
- ✅ Mock data loading
- ✅ Automatic payout calculation (Python-based, no hallucinations)
- ✅ Database storage and viewing
- ⚠️ Leg verification (requires both API keys set)

### Option 2: Command Line Interface

```bash
# Test math calculations
python src/cli.py test-math

# Process mock betting slip and save to database
python src/cli.py mock-process --store

# View database schema
python src/cli.py show-schema

# Initialize database (if needed)
python src/cli.py init-db

# Test leg verification (requires API keys)
python src/cli.py verify-leg
```

## Current Status

### Backend Tests
- ✅ Math calculator: All tests passing
- ✅ Database initialization: Working
- ✅ Mock processing: Successfully stores bets
- ✅ API keys configured:
  - Anthropic: ✅ Set
  - Tavily: ✅ Set

### Frontend
- ✅ Streamlit app running on port 8501
- ✅ All pages functional:
  - Process Betting Slip (Manual + Mock)
  - View Database
  - Verify Leg
  - Settings

## What You Can Test Now

1. **Payout Calculations**
   - Enter different American odds (+150, -110, +200, etc.)
   - Verify Python calculates correctly (no AI hallucinations)
   - Compare multi-leg parlays

2. **Database Operations**
   - Save multiple bets
   - View bet history
   - Check leg details and odds

3. **Verification System** (with API keys)
   - Select a pending leg
   - Run web search via Tavily
   - See search results stored in database

## Architecture Summary

```
┌─────────────────┐
│  Streamlit UI   │ ← You are here
│  (frontend.py)  │
└────────┬────────┘
         │
┌────────▼────────┐
│  Backend Layer  │
│  - CLI          │
│  - Database     │
│  - Math Calc    │
└────────┬────────┘
         │
┌────────▼────────┐
│  External APIs  │
│  - Anthropic    │ (Opus 5 for reasoning)
│  - Tavily       │ (Web search)
└─────────────────┘
```

## Next Steps for Production

1. **Test thoroughly** with the web UI
2. **Add vision extraction** (Google Cloud Vision or Claude Haiku)
3. **Implement full verification workflow** with Opus 5
4. **Deploy to proper platform** (Railway, Render, Fly.io)
   - Netlify won't work (Python + PostgreSQL required)

## Files Created

- `/workspace/frontend.py` - Streamlit web interface
- `/workspace/FRONTEND_README.md` - Frontend documentation
- `/workspace/TESTING_GUIDE.md` - This file

## Need Help?

Check the logs:
```bash
# Streamlit is running in background
# View logs if needed
```

Restart the frontend:
```bash
pkill -f "streamlit run"
streamlit run frontend.py --server.headless true --server.address 0.0.0.0 --server.port 8501
```
