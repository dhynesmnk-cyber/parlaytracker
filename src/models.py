"""Database models for ParlayTracker."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class Bet(Base):
    """Parent parlay details."""
    __tablename__ = 'bets'
    
    id = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False)
    sportsbook = Column(String(100), nullable=False)
    total_stake = Column(Float, nullable=False)
    final_status = Column(String(20), default='pending')  # pending, won, lost, partial
    net_profit_loss = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    legs = relationship('Leg', back_populates='bet', cascade='all, delete-orphan')
    context_entries = relationship('Context', back_populates='bet', cascade='all, delete-orphan')


class Leg(Base):
    """Individual selections within a parlay."""
    __tablename__ = 'legs'
    
    id = Column(Integer, primary_key=True)
    bet_id = Column(Integer, ForeignKey('bets.id'), nullable=False)
    sport = Column(String(50), nullable=False)
    event = Column(String(255), nullable=False)
    player = Column(String(255), nullable=True)
    prop_type = Column(String(255), nullable=False)
    american_odds = Column(Integer, nullable=False)  # e.g., +150 or -110
    leg_status = Column(String(20), default='pending')  # pending, won, lost, void
    created_at = Column(DateTime, default=datetime.utcnow)
    
    bet = relationship('Bet', back_populates='legs')
    context = relationship('Context', back_populates='leg', uselist=False, cascade='all, delete-orphan')


class Context(Base):
    """Verification data for each leg."""
    __tablename__ = 'context'
    
    id = Column(Integer, primary_key=True)
    leg_id = Column(Integer, ForeignKey('legs.id'), unique=True, nullable=False)
    bet_id = Column(Integer, ForeignKey('bets.id'), nullable=False)
    search_query = Column(Text, nullable=True)
    raw_search_results = Column(JSON, nullable=True)
    verified_outcome = Column(String(50), nullable=True)  # won, lost, void, unknown
    source_citations = Column(JSON, nullable=True)
    verification_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    leg = relationship('Leg', back_populates='context')
    bet = relationship('Bet', back_populates='context_entries')
