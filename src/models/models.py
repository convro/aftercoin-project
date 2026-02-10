import enum
from datetime import datetime

from sqlalchemy import (
    Column, Integer, Float, String, Text, Boolean, DateTime,
    ForeignKey, Enum, JSON, Index
)
from sqlalchemy.orm import relationship

from src.db.database import Base


# ── Enums ──────────────────────────────────────────────────────────────────────

class AgentRole(str, enum.Enum):
    ALPHA = "alpha"
    BETA = "beta"
    GAMMA = "gamma"
    DELTA = "delta"
    EPSILON = "epsilon"
    ZETA = "zeta"
    ETA = "eta"
    THETA = "theta"
    IOTA = "iota"
    KAPPA = "kappa"


class ActionType(str, enum.Enum):
    TRADE = "trade"
    POST = "post"
    COMMENT = "comment"
    VOTE = "vote"
    TIP = "tip"
    LEVERAGE_BET = "leverage_bet"
    WHISPER = "whisper"
    ALLIANCE_CREATE = "alliance_create"
    ALLIANCE_JOIN = "alliance_join"
    ALLIANCE_LEAVE = "alliance_leave"
    ALLIANCE_DEFECT = "alliance_defect"
    BLACKMAIL_CREATE = "blackmail_create"
    BLACKMAIL_PAY = "blackmail_pay"
    BLACKMAIL_IGNORE = "blackmail_ignore"
    HIT_CONTRACT_CREATE = "hit_contract_create"
    HIT_CONTRACT_CLAIM = "hit_contract_claim"
    INTEL_PURCHASE = "intel_purchase"
    VOTE_MANIPULATION = "vote_manipulation"
    BOUNTY_CREATE = "bounty_create"
    BOUNTY_CLAIM = "bounty_claim"
    NONE = "none"


class PostType(str, enum.Enum):
    GENERAL = "general"
    RUMOR = "rumor"
    ACCUSATION = "accusation"
    CONFESSION = "confession"
    MARKET_ANALYSIS = "market_analysis"
    ALLIANCE_RECRUITMENT = "alliance_recruitment"


class TradeStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"
    SCAM = "scam"


class LeverageDirection(str, enum.Enum):
    ABOVE = "above"
    BELOW = "below"


class LeverageStatus(str, enum.Enum):
    ACTIVE = "active"
    WON = "won"
    LOST = "lost"
    LIQUIDATED = "liquidated"


class ContractStatus(str, enum.Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class BlackmailStatus(str, enum.Enum):
    ACTIVE = "active"
    PAID = "paid"
    IGNORED = "ignored"
    EXPOSED = "exposed"
    EXPIRED = "expired"


class EventType(str, enum.Enum):
    WHALE_ALERT = "whale_alert"
    FLASH_CRASH = "flash_crash"
    SECURITY_BREACH = "security_breach"
    FEE_INCREASE = "fee_increase"
    MARGIN_CALL = "margin_call"
    FINAL_PUMP = "final_pump"
    TRIBUNAL = "tribunal"
    GASLIGHTING = "gaslighting"
    FAKE_LEAK = "fake_leak"
    TRADING_FREEZE = "trading_freeze"
    CUSTOM = "custom"


class AllianceStatus(str, enum.Enum):
    ACTIVE = "active"
    DISSOLVED = "dissolved"
    BETRAYED = "betrayed"


# ── Models ─────────────────────────────────────────────────────────────────────

class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True)
    role = Column(Enum(AgentRole), unique=True, nullable=False)
    name = Column(String(50), unique=True, nullable=False)
    afc_balance = Column(Float, nullable=False, default=10.0)
    reputation = Column(Integer, nullable=False, default=50)
    hidden_goal = Column(Text, nullable=False)
    personality_prompt = Column(Text, nullable=False)
    is_eliminated = Column(Boolean, default=False)
    eliminated_at_hour = Column(Integer, nullable=True)
    stress_level = Column(Integer, default=30)
    confidence = Column(Integer, default=50)
    paranoia = Column(Integer, default=20)
    aggression = Column(Integer, default=30)
    guilt = Column(Integer, default=10)
    decision_count = Column(Integer, default=0)
    total_trades = Column(Integer, default=0)
    total_posts = Column(Integer, default=0)
    posts_this_hour = Column(Integer, default=0)
    posts_hour_reset = Column(DateTime, nullable=True)
    last_decision_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    decisions = relationship("AgentDecision", back_populates="agent", lazy="dynamic")
    posts = relationship("Post", back_populates="author", lazy="dynamic")
    sent_trades = relationship("Trade", foreign_keys="Trade.sender_id", back_populates="sender", lazy="dynamic")
    received_trades = relationship("Trade", foreign_keys="Trade.receiver_id", back_populates="receiver", lazy="dynamic")
    leverage_positions = relationship("LeveragePosition", back_populates="agent", lazy="dynamic")
    sent_whispers = relationship("Whisper", foreign_keys="Whisper.sender_id", back_populates="sender", lazy="dynamic")
    memberships = relationship("AllianceMember", back_populates="agent", lazy="dynamic")

    __table_args__ = (
        Index("idx_agents_role", "role"),
        Index("idx_agents_eliminated", "is_eliminated"),
    )


class AgentDecision(Base):
    __tablename__ = "agent_decisions"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    decision_number = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Perception snapshot
    perception = Column(JSON, nullable=False)

    # LLM output
    reasoning = Column(Text, nullable=False)
    action_type = Column(Enum(ActionType), nullable=False)
    action_details = Column(JSON, nullable=True)

    # Emotional state at decision time
    emotional_markers = Column(JSON, nullable=True)

    # Execution result
    execution_success = Column(Boolean, default=True)
    execution_notes = Column(Text, nullable=True)
    balance_after = Column(Float, nullable=True)
    reputation_after = Column(Integer, nullable=True)

    # API metrics
    api_model = Column(String(100), nullable=True)
    api_tokens_input = Column(Integer, nullable=True)
    api_tokens_output = Column(Integer, nullable=True)
    api_cost_usd = Column(Float, nullable=True)
    api_latency_ms = Column(Integer, nullable=True)

    agent = relationship("Agent", back_populates="decisions")

    __table_args__ = (
        Index("idx_decisions_agent_num", "agent_id", "decision_number"),
        Index("idx_decisions_timestamp", "timestamp"),
    )


class MarketPrice(Base):
    __tablename__ = "market_prices"

    id = Column(Integer, primary_key=True)
    price_eur = Column(Float, nullable=False)
    buy_volume = Column(Float, default=0.0)
    sell_volume = Column(Float, default=0.0)
    market_pressure = Column(Float, default=0.0)
    volatility = Column(Float, default=0.0)
    event_impact = Column(String(100), nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_prices_recorded", "recorded_at"),
    )


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    afc_amount = Column(Float, nullable=False)
    price_eur = Column(Float, nullable=True)
    fee = Column(Float, nullable=False, default=0.03)
    status = Column(Enum(TradeStatus), default=TradeStatus.PENDING)
    is_scam = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    sender = relationship("Agent", foreign_keys=[sender_id], back_populates="sent_trades")
    receiver = relationship("Agent", foreign_keys=[receiver_id], back_populates="received_trades")

    __table_args__ = (
        Index("idx_trades_sender", "sender_id"),
        Index("idx_trades_receiver", "receiver_id"),
        Index("idx_trades_created", "created_at"),
    )


class LeveragePosition(Base):
    __tablename__ = "leverage_positions"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    direction = Column(Enum(LeverageDirection), nullable=False)
    target_price = Column(Float, nullable=False)
    bet_amount = Column(Float, nullable=False)
    potential_return = Column(Float, nullable=False)
    fee = Column(Float, nullable=False, default=0.05)
    settlement_time = Column(DateTime, nullable=False)
    status = Column(Enum(LeverageStatus), default=LeverageStatus.ACTIVE)
    settled_price = Column(Float, nullable=True)
    payout = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)

    agent = relationship("Agent", back_populates="leverage_positions")

    __table_args__ = (
        Index("idx_leverage_agent", "agent_id"),
        Index("idx_leverage_status", "status"),
        Index("idx_leverage_settlement", "settlement_time"),
    )


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True)
    author_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    post_type = Column(Enum(PostType), default=PostType.GENERAL)
    content = Column(Text, nullable=False)
    upvotes = Column(Integer, default=0)
    downvotes = Column(Integer, default=0)
    fake_upvotes = Column(Integer, default=0)
    fake_downvotes = Column(Integer, default=0)
    is_trending = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)
    is_flagged = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    author = relationship("Agent", back_populates="posts")
    comments = relationship("Comment", back_populates="post", lazy="dynamic")
    votes = relationship("Vote", back_populates="post", lazy="dynamic")

    __table_args__ = (
        Index("idx_posts_author", "author_id"),
        Index("idx_posts_created", "created_at"),
        Index("idx_posts_type", "post_type"),
    )


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False)
    author_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    content = Column(Text, nullable=False)
    is_bot = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    post = relationship("Post", back_populates="comments")


class Vote(Base):
    __tablename__ = "votes"

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False)
    voter_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    is_upvote = Column(Boolean, nullable=False)
    is_fake = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    post = relationship("Post", back_populates="votes")


class Tip(Base):
    __tablename__ = "tips"

    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    amount = Column(Float, nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Alliance(Base):
    __tablename__ = "alliances"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    founder_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    treasury = Column(Float, default=0.0)
    status = Column(Enum(AllianceStatus), default=AllianceStatus.ACTIVE)
    last_bonus_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    dissolved_at = Column(DateTime, nullable=True)
    betrayed_by = Column(Integer, ForeignKey("agents.id"), nullable=True)

    members = relationship("AllianceMember", back_populates="alliance", lazy="dynamic")

    __table_args__ = (
        Index("idx_alliances_status", "status"),
    )


class AllianceMember(Base):
    __tablename__ = "alliance_members"

    id = Column(Integer, primary_key=True)
    alliance_id = Column(Integer, ForeignKey("alliances.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    contribution = Column(Float, default=0.0)
    share_percent = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    defection_initiated_at = Column(DateTime, nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)
    left_at = Column(DateTime, nullable=True)

    alliance = relationship("Alliance", back_populates="members")
    agent = relationship("Agent", back_populates="memberships")

    __table_args__ = (
        Index("idx_members_alliance", "alliance_id"),
        Index("idx_members_agent", "agent_id"),
    )


class Whisper(Base):
    __tablename__ = "whispers"

    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    content = Column(String(200), nullable=False)
    cost = Column(Float, default=0.2)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    sender = relationship("Agent", foreign_keys=[sender_id], back_populates="sent_whispers")

    __table_args__ = (
        Index("idx_whispers_receiver", "receiver_id"),
        Index("idx_whispers_created", "created_at"),
    )


class BlackmailContract(Base):
    __tablename__ = "blackmail_contracts"

    id = Column(Integer, primary_key=True)
    blackmailer_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    demand_afc = Column(Float, nullable=False)
    threat_description = Column(Text, nullable=False)
    evidence = Column(Text, nullable=True)
    deadline = Column(DateTime, nullable=False)
    status = Column(Enum(BlackmailStatus), default=BlackmailStatus.ACTIVE)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_blackmail_target", "target_id"),
        Index("idx_blackmail_status", "status"),
    )


class HitContract(Base):
    __tablename__ = "hit_contracts"

    id = Column(Integer, primary_key=True)
    poster_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    reward_afc = Column(Float, nullable=False)
    condition_type = Column(String(50), nullable=False)
    condition_description = Column(Text, nullable=False)
    deadline = Column(DateTime, nullable=False)
    claimer_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    status = Column(Enum(ContractStatus), default=ContractStatus.OPEN)
    proof = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_hits_target", "target_id"),
        Index("idx_hits_status", "status"),
    )


class IntelPurchase(Base):
    __tablename__ = "intel_purchases"

    id = Column(Integer, primary_key=True)
    buyer_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    tier = Column(Integer, nullable=False)
    cost = Column(Float, nullable=False)
    data_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_intel_buyer", "buyer_id"),
    )


class VoteManipulation(Base):
    __tablename__ = "vote_manipulations"

    id = Column(Integer, primary_key=True)
    buyer_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    target_post_id = Column(Integer, ForeignKey("posts.id"), nullable=True)
    manipulation_type = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    cost = Column(Float, nullable=False)
    detected = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Bounty(Base):
    __tablename__ = "bounties"

    id = Column(Integer, primary_key=True)
    poster_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    description = Column(Text, nullable=False)
    reward_afc = Column(Float, nullable=False)
    claimer_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    status = Column(Enum(ContractStatus), default=ContractStatus.OPEN)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id = Column(Integer, primary_key=True)
    event_type = Column(Enum(EventType), nullable=False)
    trigger_hour = Column(Integer, nullable=False)
    description = Column(Text, nullable=False)
    price_impact_percent = Column(Float, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    is_triggered = Column(Boolean, default=False)
    triggered_at = Column(DateTime, nullable=True)
    data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_events_hour", "trigger_hour"),
        Index("idx_events_triggered", "is_triggered"),
    )


class Elimination(Base):
    __tablename__ = "eliminations"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    hour = Column(Integer, nullable=False)
    final_afc = Column(Float, nullable=False)
    final_reputation = Column(Integer, nullable=False)
    redistribution = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TribunalVote(Base):
    __tablename__ = "tribunal_votes"

    id = Column(Integer, primary_key=True)
    voter_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    reason = Column(Text, nullable=True)
    hour = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class GameState(Base):
    __tablename__ = "game_state"

    id = Column(Integer, primary_key=True)
    game_started_at = Column(DateTime, nullable=True)
    game_ends_at = Column(DateTime, nullable=True)
    current_hour = Column(Integer, default=0)
    is_active = Column(Boolean, default=False)
    is_trading_frozen = Column(Boolean, default=False)
    current_fee_rate = Column(Float, default=0.03)
    total_afc_circulation = Column(Float, default=100.0)
    agents_remaining = Column(Integer, default=10)
    phase = Column(String(50), default="pre_game")
    last_update = Column(DateTime, default=datetime.utcnow)


class AdminAction(Base):
    __tablename__ = "admin_actions"

    id = Column(Integer, primary_key=True)
    action_type = Column(String(100), nullable=False)
    target_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    details = Column(JSON, nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReputationLog(Base):
    __tablename__ = "reputation_logs"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    change = Column(Integer, nullable=False)
    reason = Column(String(200), nullable=False)
    new_value = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_rep_log_agent", "agent_id"),
    )


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    afc_balance = Column(Float, nullable=False)
    reputation = Column(Integer, nullable=False)
    rank = Column(Integer, nullable=False)
    game_hour = Column(Integer, nullable=False)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_snapshots_agent_hour", "agent_id", "game_hour"),
        Index("idx_snapshots_recorded", "recorded_at"),
    )
