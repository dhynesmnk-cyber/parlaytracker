#!/usr/bin/env python3
"""
Streamlit Frontend for ParlayTracker.
Test the backend pipeline with a simple web UI.
"""
import os
import sys
import json
import tempfile
from datetime import datetime
from typing import Optional

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

import streamlit as st
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Page config
st.set_page_config(
    page_title="ParlayTracker",
    page_icon="📊",
    layout="wide"
)

def main():
    st.title("📊 ParlayTracker")
    st.markdown("---")
    
    # Sidebar
    st.sidebar.title("Navigation")
    menu = st.sidebar.radio(
        "Choose Action",
        ["Process Betting Slip", "View Database", "Verify Leg", "Settings"]
    )
    
    if menu == "Process Betting Slip":
        show_process_slip()
    elif menu == "View Database":
        show_database_view()
    elif menu == "Verify Leg":
        show_verification()
    elif menu == "Settings":
        show_settings()

def show_process_slip():
    """Process a betting slip (manual entry or mock)."""
    st.header("Process Betting Slip")
    
    # Import modules here to avoid initialization issues
    from src.database import init_db, get_session
    from src.models import Bet, Leg
    from src.math_calculator import calculate_parlay_payout
    
    tab1, tab2 = st.tabs(["Manual Entry", "Use Mock Data"])
    
    with tab1:
        st.subheader("Enter Bet Details")
        
        col1, col2 = st.columns(2)
        with col1:
            sportsbook = st.selectbox(
                "Sportsbook",
                ["DraftKings", "FanDuel", "BetMGM", "Caesars", "PointsBet", "Other"]
            )
            bet_date = st.date_input("Date", value=datetime.now())
            total_stake = st.number_input("Total Stake ($)", min_value=0.0, value=100.0)
        
        with col2:
            num_legs = st.number_input("Number of Legs", min_value=1, max_value=15, value=3)
        
        st.subheader("Leg Details")
        legs_data = []
        
        for i in range(int(num_legs)):
            with st.expander(f"Leg {i+1}", expanded=(i==0)):
                col1, col2 = st.columns(2)
                with col1:
                    sport = st.selectbox(
                        "Sport",
                        ["NFL", "NBA", "MLB", "NHL", "Soccer", "Other"],
                        key=f"sport_{i}"
                    )
                    event = st.text_input("Event/Game", key=f"event_{i}")
                    player = st.text_input("Player (if applicable)", key=f"player_{i}")
                
                with col2:
                    prop_type = st.text_input("Prop Type/Bet Description", key=f"prop_{i}")
                    american_odds = st.number_input(
                        "American Odds",
                        min_value=-10000,
                        max_value=10000,
                        value=-110,
                        key=f"odds_{i}"
                    )
                    
                    legs_data.append({
                        "sport": sport,
                        "event": event,
                        "player": player,
                        "prop_type": prop_type,
                        "american_odds": int(american_odds)
                    })
        
        if st.button("Calculate Payout", type="primary"):
            if legs_data and all(leg['event'] for leg in legs_data):
                odds_list = [leg['american_odds'] for leg in legs_data]
                result = calculate_parlay_payout(total_stake, odds_list)
                
                st.success("✅ Calculation Complete!")
                
                col1, col2, col3 = st.columns(3)
                col1.metric("Multiplier", f"{result['multiplier']}x")
                col2.metric("Total Payout", f"${result['total_payout']:.2f}")
                col3.metric("Net Profit", f"${result['net_profit']:.2f}")
                
                if st.button("Save to Database"):
                    save_bet(sportsbook, bet_date, total_stake, legs_data, result)
            else:
                st.error("Please fill in all required fields.")
    
    with tab2:
        st.subheader("Use Mock Data")
        st.write("Load pre-defined mock betting slip for testing.")
        
        if st.button("Load Mock Data"):
            mock_data = {
                "sportsbook": "DraftKings",
                "date": "2024-01-15",
                "total_stake": 100.0,
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
            
            st.json(mock_data)
            
            odds_list = [leg['american_odds'] for leg in mock_data['legs']]
            result = calculate_parlay_payout(mock_data['total_stake'], odds_list)
            
            st.info(f"**Calculated Payout:** Multiplier {result['multiplier']}x, Total ${result['total_payout']:.2f}, Net Profit ${result['net_profit']:.2f}")
            
            if st.button("Save Mock Data to Database"):
                engine = init_db()
                session = get_session(engine)
                try:
                    bet = Bet(
                        date=datetime.strptime(mock_data['date'], '%Y-%m-%d'),
                        sportsbook=mock_data['sportsbook'],
                        total_stake=mock_data['total_stake'],
                        net_profit_loss=result['net_profit'],
                        final_status='pending'
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
                    st.success(f"✅ Saved to database! Bet ID: {bet.id}")
                except Exception as e:
                    session.rollback()
                    st.error(f"❌ Error: {e}")
                finally:
                    session.close()

def save_bet(sportsbook, bet_date, total_stake, legs_data, calculation_result):
    """Save bet to database."""
    from src.database import init_db, get_session
    from src.models import Bet, Leg
    
    engine = init_db()
    session = get_session(engine)
    try:
        bet = Bet(
            date=datetime.combine(bet_date, datetime.min.time()),
            sportsbook=sportsbook,
            total_stake=total_stake,
            net_profit_loss=calculation_result['net_profit'],
            final_status='pending'
        )
        session.add(bet)
        session.flush()
        
        for leg_data in legs_data:
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
        st.success(f"✅ Saved to database! Bet ID: {bet.id}")
    except Exception as e:
        session.rollback()
        st.error(f"❌ Error: {e}")
    finally:
        session.close()

def show_database_view():
    """View bets stored in database."""
    st.header("Database View")
    
    from src.database import init_db, get_session
    from src.models import Bet, Leg, Context
    
    engine = init_db()
    session = get_session(engine)
    try:
        bets = session.query(Bet).order_by(Bet.created_at.desc()).all()
        
        if not bets:
            st.info("No bets in database yet. Process a betting slip first.")
            return
        
        st.subheader(f"All Bets ({len(bets)} total)")
        
        for bet in bets:
            with st.expander(f"Bet #{bet.id} - {bet.sportsbook} - ${bet.total_stake}", expanded=False):
                st.write(f"**Date:** {bet.date.strftime('%Y-%m-%d') if bet.date else 'N/A'}")
                st.write(f"**Status:** {bet.final_status}")
                st.write(f"**Stake:** ${bet.total_stake}")
                st.write(f"**Net Profit/Loss:** ${bet.net_profit_loss:.2f}" if bet.net_profit_loss else "**Net Profit/Loss:** Pending")
                
                legs = session.query(Leg).filter(Leg.bet_id == bet.id).all()
                st.write("**Legs:**")
                for leg in legs:
                    st.write(f"- {leg.sport}: {leg.event} | {leg.player or 'N/A'} | {leg.prop_type} ({leg.american_odds:+d})")
                
                # Check for context/verification
                contexts = session.query(Context).filter(Context.bet_id == bet.id).all()
                if contexts:
                    st.write("**Verification Notes:**")
                    for ctx in contexts:
                        st.write(f"- Leg {ctx.leg_id}: {ctx.verified_outcome}")
                        if ctx.verification_notes:
                            st.write(f"  _{ctx.verification_notes}_")
    finally:
        session.close()

def show_verification():
    """Verify a specific leg."""
    st.header("Leg Verification")
    
    from src.database import init_db, get_session
    from src.models import Bet, Leg, Context
    
    # Try importing verification components
    try:
        from src.search_agent import SearchAgent
        from src.verification_engine import VerificationEngine
        VERIFICATION_AVAILABLE = True
    except ImportError as e:
        st.warning(f"⚠️ Verification module not available: {e}")
        return
    
    if not os.getenv('TAVILY_API_KEY'):
        st.error("❌ TAVILY_API_KEY not set. Check Settings page.")
        return
    
    if not os.getenv('ANTHROPIC_API_KEY'):
        st.error("❌ ANTHROPIC_API_KEY not set. Check Settings page.")
        return
    
    engine = init_db()
    session = get_session(engine)
    try:
        # Get pending legs
        legs = session.query(Leg).join(Bet).filter(
            Bet.final_status == 'pending'
        ).all()
        
        if not legs:
            st.info("No pending legs to verify. Process a bet first.")
            return
        
        st.subheader("Select Leg to Verify")
        leg_options = [f"#{leg.id}: {leg.player or 'N/A'} - {leg.prop_type}" for leg in legs]
        selected = st.selectbox("Leg", leg_options)
        
        if selected:
            leg_idx = leg_options.index(selected)
            leg = legs[leg_idx]
            
            st.write(f"**Sport:** {leg.sport}")
            st.write(f"**Event:** {leg.event}")
            st.write(f"**Player:** {leg.player or 'N/A'}")
            st.write(f"**Prop:** {leg.prop_type}")
            st.write(f"**Odds:** {leg.american_odds:+d}")
            
            if st.button("Run Verification"):
                with st.spinner("Searching and analyzing..."):
                    try:
                        ver_engine = VerificationEngine()
                        
                        # Extract game date (simplified - would need better parsing in production)
                        game_date = datetime.now().strftime('%Y-%m-%d')
                        
                        test_leg = {
                            "sport": leg.sport,
                            "event": leg.event,
                            "player": leg.player,
                            "prop_type": leg.prop_type,
                            "american_odds": leg.american_odds
                        }
                        
                        # Search
                        search_results = ver_engine.search_agent.search_leg_outcome(test_leg, game_date)
                        
                        st.subheader("Search Results")
                        for i, result in enumerate(search_results['results'][:3], 1):
                            with st.expander(f"Result {i}: {result.get('title', 'No title')}"):
                                st.write(f"**URL:** {result.get('url', 'N/A')}")
                                st.write(result.get('content', 'No content'))
                        
                        # Store context
                        context = Context(
                            leg_id=leg.id,
                            bet_id=leg.bet_id,
                            search_query=search_results['query'],
                            raw_search_results=json.dumps(search_results['results']),
                            verified_outcome='unknown',
                            verification_notes=f"Search completed. {len(search_results['results'])} results found."
                        )
                        session.add(context)
                        session.commit()
                        
                        st.success("✅ Verification search complete! Results stored in database.")
                        
                    except Exception as e:
                        st.error(f"❌ Verification failed: {str(e)}")
    finally:
        session.close()

def show_settings():
    """Show and configure settings."""
    st.header("Settings")
    
    from src.database import init_db, get_session
    from src.models import Bet, Leg
    
    st.subheader("API Keys")
    
    anthropic_key = os.getenv('ANTHROPIC_API_KEY', '')
    tavily_key = os.getenv('TAVILY_API_KEY', '')
    
    col1, col2 = st.columns(2)
    with col1:
        st.write("**Anthropic API Key:**")
        if anthropic_key:
            st.success("✅ Set")
            st.text_input("Key", value=anthropic_key[:10] + "...", disabled=True)
        else:
            st.error("❌ Not set")
    
    with col2:
        st.write("**Tavily API Key:**")
        if tavily_key:
            st.success("✅ Set")
            st.text_input("Key", value=tavily_key[:10] + "...", disabled=True)
        else:
            st.error("❌ Not set")
    
    st.info("💡 Set these keys in your `.env` file:")
    st.code("""
ANTHROPIC_API_KEY=sk-ant-api03-...
TAVILY_API_KEY=tvly-dev-...
""", language="bash")
    
    st.subheader("Database Info")
    engine = init_db()
    st.write(f"**Database URL:** {engine.url}")
    
    session = get_session(engine)
    try:
        bet_count = session.query(Bet).count()
        leg_count = session.query(Leg).count()
        st.write(f"**Total Bets:** {bet_count}")
        st.write(f"**Total Legs:** {leg_count}")
    finally:
        session.close()

if __name__ == "__main__":
    main()
