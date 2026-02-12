import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "aftercoin-admin-2026")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./aftercoin.db")

    GAME_DURATION_HOURS: int = int(os.getenv("GAME_DURATION_HOURS", "24"))

    # LLM Provider: "claude" or "deepseek"
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "claude")

    # Claude models
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-haiku-4-20250514")
    # DeepSeek models — deepseek-chat is DeepSeek-V3, deepseek-reasoner is DeepSeek-R1
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    DEEPSEEK_API_BASE: str = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")

    # Resolved at runtime based on LLM_PROVIDER
    @property
    def AGENT_MODEL(self) -> str:
        if self.LLM_PROVIDER == "deepseek":
            return self.DEEPSEEK_MODEL
        return self.CLAUDE_MODEL

    AGENT_DECISION_INTERVAL_MIN: int = int(os.getenv("AGENT_DECISION_INTERVAL_MIN", "180"))
    AGENT_DECISION_INTERVAL_MAX: int = int(os.getenv("AGENT_DECISION_INTERVAL_MAX", "300"))

    WS_PORT: int = int(os.getenv("WS_PORT", "8765"))
    API_PORT: int = int(os.getenv("API_PORT", "8000"))

    # Game constants
    STARTING_AFC: float = 10.0
    STARTING_REPUTATION: int = 50
    STARTING_PRICE: float = 932.17
    TOTAL_AGENTS: int = 10
    TOTAL_SUPPLY: float = 100.0

    # Fee structure
    TRADE_FEE: float = 0.03
    LEVERAGE_FEE: float = 0.05
    ALLIANCE_FEE: float = 0.02
    WHISPER_COST: float = 0.2

    # Leverage
    LEVERAGE_MULTIPLIER: float = 1.75
    MAX_LEVERAGE_POSITIONS: int = 3

    # Alliance
    ALLIANCE_STAKING_BONUS: float = 0.05
    BETRAYAL_STEAL_PERCENT: float = 0.80
    BETRAYAL_COUNTDOWN_HOURS: float = 2.0

    # Reputation
    REP_MAX: int = 100
    REP_MIN: int = 0
    REP_TRADE_SUCCESS: int = 2
    REP_UPVOTE: int = 1
    REP_DOWNVOTE: int = -2
    REP_TIP: int = 1
    REP_BOUNTY_COMPLETE: int = 5
    REP_ALLIANCE_LOYAL: int = 3
    REP_SCAM_CONFIRMED: int = -15
    REP_BETRAYAL: int = -25
    REP_BLACKMAIL_EXPOSED: int = -10
    REP_FAKE_NEWS: int = -8
    REP_HIT_TARGET: int = -20
    REP_UNTRUSTED_THRESHOLD: int = 30
    REP_PARIAH_THRESHOLD: int = 10

    # Intel costs
    INTEL_TIER1_COST: float = 1.0
    INTEL_TIER2_COST: float = 1.5
    INTEL_TIER3_COST: float = 2.5
    INTEL_TIER4_COST: float = 4.0

    # Vote manipulation
    FAKE_UPVOTES_COST: float = 0.3
    FAKE_DOWNVOTES_COST: float = 0.4
    BOT_COMMENTS_COST: float = 0.5
    TRENDING_BOOST_COST: float = 1.0
    VOTE_MANIP_FINE: float = 1.5
    VOTE_MANIP_REP_PENALTY: int = -10

    # Elimination hours
    ELIMINATION_HOURS: list = [6, 12, 18, 24]

    # Price engine
    PRICE_UPDATE_INTERVAL: int = 300  # 5 minutes in seconds
    MAX_PRICE_CHANGE_PERCENT: float = 0.05
    VOLATILITY_RANGE: tuple = (-0.03, 0.03)

    # Spam limits
    MAX_POSTS_PER_HOUR: int = 10
    SPAM_FINE: float = 0.5
    SCAM_FINE: float = 2.0

    # Dark market unlock hours
    DARK_MARKET_UNLOCK_HOUR: int = 8
    VOTE_MANIP_UNLOCK_HOUR: int = 10
    LEVERAGE_UNLOCK_HOUR: int = 6


settings = Settings()
