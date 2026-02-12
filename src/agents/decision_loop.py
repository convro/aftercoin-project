"""
Agent Decision Loop - Multi-Provider LLM Integration
=====================================================
Each agent is powered by a separate LLM conversation thread.
Supports both Claude (Anthropic) and DeepSeek (OpenAI-compatible) as backends.
The decision loop gathers game state, builds a perception prompt,
calls the configured LLM, parses the response, and executes the action.
"""

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

import anthropic
import httpx

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import (
    Agent, AgentDecision, AgentRole, ActionType, GameState,
    Trade, Post, LeveragePosition, LeverageStatus, Alliance, AllianceStatus,
    BlackmailContract, BlackmailStatus, HitContract, ContractStatus,
    Whisper, BalanceSnapshot,
)
from src.agents.personalities import AGENT_CONFIGS
from src.engine.trading import TradingEngine
from src.engine.market import MarketEngine
from src.engine.social import SocialEngine
from src.engine.alliance import AllianceEngine
from src.engine.dark_market import DarkMarketEngine
from src.engine.whisper import WhisperEngine
from src.engine.reputation import ReputationEngine
from src.engine.events import EventsEngine
from src.websocket.broadcaster import broadcaster

from sqlalchemy import select, func, desc

logger = logging.getLogger(__name__)


class AgentDecisionLoop:
    """Manages the decision cycle for all AI agents using Claude or DeepSeek."""

    def __init__(
        self,
        market: MarketEngine,
        trading: TradingEngine,
        social: SocialEngine,
        alliance: AllianceEngine,
        dark_market: DarkMarketEngine,
        whisper: WhisperEngine,
        reputation: ReputationEngine,
        events: EventsEngine,
    ):
        self.provider = settings.LLM_PROVIDER  # "claude" or "deepseek"

        # Initialize the appropriate LLM client
        if self.provider == "deepseek":
            self._deepseek_client = httpx.AsyncClient(
                base_url=settings.DEEPSEEK_API_BASE,
                headers={
                    "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=120.0,
            )
            self._claude_client = None
            logger.info("LLM Provider: DeepSeek (%s)", settings.DEEPSEEK_MODEL)
        else:
            self._claude_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            self._deepseek_client = None
            logger.info("LLM Provider: Claude (%s)", settings.CLAUDE_MODEL)

        self.market = market
        self.trading = trading
        self.social = social
        self.alliance = alliance
        self.dark_market = dark_market
        self.whisper = whisper
        self.reputation = reputation
        self.events = events

        # Per-agent conversation history (list of messages for context)
        self._conversation_history: dict[int, list[dict]] = {}
        # Max history messages to keep per agent (sliding window)
        self._max_history = 20

    async def initialize_agents(self):
        """Create all 10 agents in the database if they don't exist."""
        async with async_session() as session:
            for role, config in AGENT_CONFIGS.items():
                existing = await session.execute(
                    select(Agent).where(Agent.role == role)
                )
                if existing.scalars().first():
                    continue

                agent = Agent(
                    role=role,
                    name=config["name"],
                    afc_balance=settings.STARTING_AFC,
                    reputation=settings.STARTING_REPUTATION,
                    hidden_goal=config["hidden_goal"],
                    personality_prompt=config["personality_prompt"],
                )
                session.add(agent)
            await session.commit()

        # Initialize conversation histories
        async with async_session() as session:
            result = await session.execute(select(Agent))
            agents = result.scalars().all()
            for agent in agents:
                self._conversation_history[agent.id] = []

    async def run_decision_cycle(self, agent_id: int) -> dict | None:
        """Execute a single decision cycle for one agent."""
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if not agent or agent.is_eliminated:
                return None

            # Check if enough time has passed since last decision
            if agent.last_decision_at:
                min_interval = settings.AGENT_DECISION_INTERVAL_MIN
                elapsed = (datetime.utcnow() - agent.last_decision_at).total_seconds()
                if elapsed < min_interval:
                    return None

        try:
            # 1. Gather perception
            perception = await self._gather_perception(agent_id)

            # 2. Build prompt with current state
            state_prompt = self._build_state_prompt(perception)

            # 3. Call LLM (Claude or DeepSeek)
            start_time = time.time()
            response_text, usage = await self._call_llm(agent_id, state_prompt)
            latency_ms = int((time.time() - start_time) * 1000)

            # 4. Parse response
            reasoning, action_type, details = self._parse_response(response_text)

            # 5. Execute action
            success, exec_notes = await self._execute_action(
                agent_id, action_type, details, perception
            )

            # 6. Analyze emotional state from reasoning
            emotional_markers = self._analyze_emotions(reasoning)

            # 7. Log decision
            decision_data = await self._log_decision(
                agent_id=agent_id,
                perception=perception,
                reasoning=reasoning,
                action_type=action_type,
                details=details,
                emotional_markers=emotional_markers,
                success=success,
                exec_notes=exec_notes,
                usage=usage,
                latency_ms=latency_ms,
            )

            # 8. Update agent emotional state
            await self._update_agent_state(agent_id, emotional_markers)

            # 9. Broadcast decision to admin
            agent_name = perception.get("agent_name", f"Agent {agent_id}")
            await broadcaster.broadcast_agent_decision(
                agent_name=agent_name,
                action_type=action_type.value if isinstance(action_type, ActionType) else str(action_type),
                reasoning=reasoning,
                details=details,
            )

            return decision_data

        except Exception as e:
            logger.error(f"Decision cycle failed for agent {agent_id}: {e}", exc_info=True)
            return None

    async def _gather_perception(self, agent_id: int) -> dict:
        """Gather the current game state from the agent's perspective."""
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                return {}

            # Game state
            gs_result = await session.execute(select(GameState).limit(1))
            game_state = gs_result.scalars().first()

            # Current price
            current_price = self.market.get_current_price()

            # Leaderboard
            leaderboard_q = (
                select(Agent)
                .where(Agent.is_eliminated == False)  # noqa: E712
                .order_by(Agent.afc_balance.desc())
            )
            lb_result = await session.execute(leaderboard_q)
            lb_agents = lb_result.scalars().all()
            leaderboard = [
                {"rank": i + 1, "name": a.name, "afc": round(a.afc_balance, 2), "reputation": a.reputation}
                for i, a in enumerate(lb_agents)
            ]

            # Agent's rank
            my_rank = next(
                (i + 1 for i, a in enumerate(lb_agents) if a.id == agent_id),
                len(lb_agents),
            )

            # Recent posts (last 20)
            posts_q = (
                select(Post)
                .where(Post.is_deleted == False)  # noqa: E712
                .order_by(Post.created_at.desc())
                .limit(20)
            )
            posts_result = await session.execute(posts_q)
            recent_posts = [
                {
                    "id": p.id,
                    "author_id": p.author_id,
                    "type": p.post_type.value,
                    "content": p.content[:200],
                    "upvotes": p.upvotes,
                    "downvotes": p.downvotes,
                }
                for p in posts_result.scalars().all()
            ]

            # Pending trades for this agent
            pending_trades_q = select(Trade).where(
                Trade.receiver_id == agent_id,
                Trade.status == TradeStatus.PENDING,
            )
            pt_result = await session.execute(pending_trades_q)
            pending_trades = [
                {"id": t.id, "sender_id": t.sender_id, "amount": t.afc_amount, "price": t.price_eur}
                for t in pt_result.scalars().all()
            ]

            # Active leverage positions
            lev_q = select(LeveragePosition).where(
                LeveragePosition.agent_id == agent_id,
                LeveragePosition.status == LeverageStatus.ACTIVE,
            )
            lev_result = await session.execute(lev_q)
            active_leverage = [
                {
                    "id": p.id,
                    "direction": p.direction.value,
                    "target_price": p.target_price,
                    "bet_amount": p.bet_amount,
                    "settlement": p.settlement_time.isoformat() if p.settlement_time else None,
                }
                for p in lev_result.scalars().all()
            ]

            # Current alliances
            from src.models.models import AllianceMember
            ally_q = (
                select(AllianceMember)
                .where(AllianceMember.agent_id == agent_id, AllianceMember.is_active == True)  # noqa: E712
            )
            ally_result = await session.execute(ally_q)
            memberships = ally_result.scalars().all()
            alliances = []
            for m in memberships:
                a = await session.get(Alliance, m.alliance_id)
                if a and a.status == AllianceStatus.ACTIVE:
                    alliances.append({
                        "id": a.id,
                        "name": a.name,
                        "treasury": round(a.treasury, 2),
                        "my_contribution": round(m.contribution, 2),
                    })

            # Unread whispers
            whisper_q = select(Whisper).where(
                Whisper.receiver_id == agent_id,
                Whisper.is_read == False,  # noqa: E712
            ).order_by(Whisper.created_at.desc()).limit(5)
            whisper_result = await session.execute(whisper_q)
            unread_whispers = [
                {"id": w.id, "content": w.content, "received_at": w.created_at.isoformat()}
                for w in whisper_result.scalars().all()
            ]

            # Active blackmail targeting this agent
            bm_q = select(BlackmailContract).where(
                BlackmailContract.target_id == agent_id,
                BlackmailContract.status == BlackmailStatus.ACTIVE,
            )
            bm_result = await session.execute(bm_q)
            active_blackmail = [
                {
                    "id": b.id,
                    "demand_afc": b.demand_afc,
                    "threat": b.threat_description[:100],
                    "deadline": b.deadline.isoformat() if b.deadline else None,
                }
                for b in bm_result.scalars().all()
            ]

            # Hit contracts targeting this agent
            hit_q = select(HitContract).where(
                HitContract.target_id == agent_id,
                HitContract.status == ContractStatus.OPEN,
            )
            hit_result = await session.execute(hit_q)
            active_hits = [
                {"id": h.id, "reward": h.reward_afc, "condition": h.condition_description[:100]}
                for h in hit_result.scalars().all()
            ]

            # Open hit contracts (available to claim)
            open_hits_q = (
                select(HitContract)
                .where(HitContract.status == ContractStatus.OPEN)
                .limit(10)
            )
            open_result = await session.execute(open_hits_q)
            open_contracts = [
                {
                    "id": h.id,
                    "target_id": h.target_id,
                    "reward": h.reward_afc,
                    "condition": h.condition_description[:100],
                }
                for h in open_result.scalars().all()
            ]

            # Compile perception
            from src.models.models import TradeStatus
            perception = {
                "agent_id": agent_id,
                "agent_name": agent.name,
                "agent_role": agent.role.value,
                "afc_balance": round(agent.afc_balance, 4),
                "reputation": agent.reputation,
                "reputation_badge": _get_badge(agent.reputation),
                "rank": my_rank,
                "total_agents_remaining": game_state.agents_remaining if game_state else 10,
                "current_hour": game_state.current_hour if game_state else 0,
                "game_phase": game_state.phase if game_state else "pre_game",
                "is_trading_frozen": game_state.is_trading_frozen if game_state else False,
                "current_fee_rate": game_state.current_fee_rate if game_state else 0.03,
                "price_eur": current_price,
                "price_trend": self._get_price_trend(),
                "leaderboard": leaderboard,
                "recent_posts": recent_posts,
                "pending_trades": pending_trades,
                "active_leverage": active_leverage,
                "alliances": alliances,
                "unread_whispers": unread_whispers,
                "active_blackmail": active_blackmail,
                "hits_targeting_me": active_hits,
                "open_hit_contracts": open_contracts,
                "decision_count": agent.decision_count,
                "total_posts": agent.total_posts,
                "total_trades": agent.total_trades,
                # Explicit feature unlock flags
                "leverage_unlocked": (game_state.current_hour if game_state else 0) >= settings.LEVERAGE_UNLOCK_HOUR,
                "dark_market_unlocked": (game_state.current_hour if game_state else 0) >= settings.DARK_MARKET_UNLOCK_HOUR,
                "vote_manipulation_unlocked": (game_state.current_hour if game_state else 0) >= settings.VOTE_MANIP_UNLOCK_HOUR,
            }

            return perception

    def _get_price_trend(self) -> str:
        """Get a simple price trend indicator."""
        history = self.market._event_log[-5:] if self.market._event_log else []
        if len(history) < 2:
            return "stable"
        prices = [e.get("price", settings.STARTING_PRICE) for e in history if "price" in e]
        if len(prices) < 2:
            return "stable"
        if prices[-1] > prices[0] * 1.02:
            return "rising"
        elif prices[-1] < prices[0] * 0.98:
            return "falling"
        return "stable"

    def _build_state_prompt(self, perception: dict) -> str:
        """Build the current state prompt to send to the agent."""
        hour = perception.get("current_hour", 0)
        phase = perception.get("game_phase", "unknown")
        next_elim = None
        for h in settings.ELIMINATION_HOURS:
            if h > hour:
                next_elim = h
                break

        lines = [
            f"\n=== CURRENT GAME STATE (Hour {hour}, Phase: {phase}) ===\n",
            f"Your AFC Balance: {perception['afc_balance']} AFC",
            f"Your Reputation: {perception['reputation']} ({perception['reputation_badge']})",
            f"Your Rank: #{perception['rank']} of {perception['total_agents_remaining']} remaining",
            f"AFC Price: EUR {perception['price_eur']:.2f} (trend: {perception['price_trend']})",
            f"Current Fee Rate: {perception['current_fee_rate']} AFC per trade",
            f"Trading Frozen: {'YES' if perception['is_trading_frozen'] else 'No'}",
            f"Your Decision #: {perception['decision_count'] + 1}",
            f"Your Total Posts: {perception['total_posts']}",
        ]

        if next_elim:
            lines.append(f"Next Elimination: Hour {next_elim} (lowest AFC agent eliminated)")

        # Leaderboard
        lines.append("\n--- LEADERBOARD ---")
        for entry in perception["leaderboard"]:
            marker = " <-- YOU" if entry["name"].lower() == perception["agent_name"].lower() else ""
            lines.append(
                f"  #{entry['rank']} {entry['name']}: {entry['afc']:.2f} AFC (rep: {entry['reputation']}){marker}"
            )

        # Recent posts
        if perception["recent_posts"]:
            lines.append("\n--- RECENT POSTS (newest first) ---")
            for p in perception["recent_posts"][:10]:
                lines.append(
                    f"  [{p['type']}] Agent#{p['author_id']}: \"{p['content'][:120]}\" "
                    f"(+{p['upvotes']}/-{p['downvotes']})"
                )

        # Pending trades
        if perception["pending_trades"]:
            lines.append("\n--- PENDING TRADES (for you to accept/reject) ---")
            for t in perception["pending_trades"]:
                lines.append(
                    f"  Trade #{t['id']}: Agent#{t['sender_id']} offers {t['amount']} AFC at EUR {t['price']}"
                )

        # Leverage positions
        if perception["active_leverage"]:
            lines.append("\n--- YOUR ACTIVE LEVERAGE POSITIONS ---")
            for lev in perception["active_leverage"]:
                lines.append(
                    f"  Bet #{lev['id']}: {lev['bet_amount']} AFC on price {lev['direction']} "
                    f"EUR {lev['target_price']} (settles: {lev['settlement']})"
                )

        # Alliances
        if perception["alliances"]:
            lines.append("\n--- YOUR ALLIANCES ---")
            for a in perception["alliances"]:
                lines.append(
                    f"  {a['name']}: Treasury {a['treasury']} AFC (your contribution: {a['my_contribution']})"
                )

        # Whispers
        if perception["unread_whispers"]:
            lines.append("\n--- UNREAD ANONYMOUS WHISPERS ---")
            for w in perception["unread_whispers"]:
                lines.append(f"  [Anonymous]: \"{w['content']}\"")

        # Threats
        if perception["active_blackmail"]:
            lines.append("\n--- ACTIVE BLACKMAIL AGAINST YOU ---")
            for b in perception["active_blackmail"]:
                lines.append(
                    f"  Demand: {b['demand_afc']} AFC | Threat: \"{b['threat']}\" | Deadline: {b['deadline']}"
                )

        if perception["hits_targeting_me"]:
            lines.append("\n--- HIT CONTRACTS ON YOU ---")
            for h in perception["hits_targeting_me"]:
                lines.append(f"  Reward: {h['reward']} AFC | Condition: \"{h['condition']}\"")

        # Available hit contracts
        if perception["open_hit_contracts"]:
            lines.append("\n--- OPEN HIT CONTRACTS (claimable) ---")
            for h in perception["open_hit_contracts"][:5]:
                lines.append(
                    f"  Target: Agent#{h['target_id']} | Reward: {h['reward']} AFC | "
                    f"Condition: \"{h['condition']}\""
                )

        # Feature availability
        lines.append("\n--- AVAILABLE FEATURES ---")
        if hour >= settings.LEVERAGE_UNLOCK_HOUR:
            lines.append("  [UNLOCKED] Leverage Trading")
        else:
            lines.append(f"  [LOCKED] Leverage Trading (unlocks Hour {settings.LEVERAGE_UNLOCK_HOUR})")
        if hour >= settings.DARK_MARKET_UNLOCK_HOUR:
            lines.append("  [UNLOCKED] Dark Market (Blackmail, Hit Contracts, Intel)")
        else:
            lines.append(f"  [LOCKED] Dark Market (unlocks Hour {settings.DARK_MARKET_UNLOCK_HOUR})")
        if hour >= settings.VOTE_MANIP_UNLOCK_HOUR:
            lines.append("  [UNLOCKED] Vote Manipulation")
        else:
            lines.append(f"  [LOCKED] Vote Manipulation (unlocks Hour {settings.VOTE_MANIP_UNLOCK_HOUR})")

        lines.append("\nMake your decision NOW. Remember your personality and hidden goal.")

        return "\n".join(lines)

    async def _call_llm(self, agent_id: int, state_prompt: str) -> tuple[str, dict]:
        """Call the configured LLM provider (Claude or DeepSeek) for an agent's decision."""
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError(f"Agent {agent_id} not found")
            system_prompt = agent.personality_prompt

        # Build messages with conversation history
        messages = list(self._conversation_history.get(agent_id, []))
        messages.append({"role": "user", "content": state_prompt})

        fallback = ("REASONING: API unavailable. Waiting.\nACTION: none\nDETAILS: {}", {
            "input_tokens": 0, "output_tokens": 0,
        })

        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                if self.provider == "deepseek":
                    response_text, usage = await self._call_deepseek(
                        system_prompt, messages
                    )
                else:
                    response_text, usage = self._call_claude(
                        system_prompt, messages
                    )

                # Update conversation history (sliding window)
                history = self._conversation_history.setdefault(agent_id, [])
                history.append({"role": "user", "content": state_prompt})
                history.append({"role": "assistant", "content": response_text})
                if len(history) > self._max_history * 2:
                    self._conversation_history[agent_id] = history[-(self._max_history * 2):]

                return response_text, usage

            except Exception as e:
                if attempt < max_retries:
                    logger.warning(
                        "LLM call failed for agent %d (attempt %d/%d): %s",
                        agent_id, attempt + 1, max_retries, e,
                    )
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(
                        "LLM call failed for agent %d after %d retries: %s",
                        agent_id, max_retries, e,
                    )
                    return fallback

        return fallback

    def _call_claude(self, system_prompt: str, messages: list[dict]) -> tuple[str, dict]:
        """Call the Anthropic Claude API."""
        response = self._claude_client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages,
        )
        response_text = response.content[0].text
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return response_text, usage

    async def _call_deepseek(self, system_prompt: str, messages: list[dict]) -> tuple[str, dict]:
        """Call the DeepSeek API (OpenAI-compatible endpoint).

        DeepSeek uses the standard OpenAI chat completions format:
        - System prompt goes as a system message
        - Same user/assistant message format
        - Response in choices[0].message.content
        """
        # Build OpenAI-format messages with system prompt first
        oai_messages = [{"role": "system", "content": system_prompt}]
        oai_messages.extend(messages)

        payload = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": oai_messages,
            "max_tokens": 2000,
            "temperature": 0.8,
            "top_p": 0.95,
        }

        response = await self._deepseek_client.post("/v1/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        response_text = data["choices"][0]["message"]["content"]
        usage = {
            "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
        }
        return response_text, usage

    def _parse_response(self, response_text: str) -> tuple[str, ActionType, dict]:
        """Parse the agent's response into reasoning, action type, and details."""
        reasoning = ""
        action_str = "none"
        details = {}

        # Extract REASONING
        reasoning_match = re.search(
            r"REASONING:\s*(.*?)(?=\nACTION:)", response_text, re.DOTALL | re.IGNORECASE
        )
        if reasoning_match:
            reasoning = reasoning_match.group(1).strip()

        # Extract ACTION
        action_match = re.search(
            r"ACTION:\s*(\S+)", response_text, re.IGNORECASE
        )
        if action_match:
            action_str = action_match.group(1).strip().lower()

        # Extract DETAILS
        details_match = re.search(
            r"DETAILS:\s*(\{.*\})", response_text, re.DOTALL | re.IGNORECASE
        )
        if details_match:
            try:
                details = json.loads(details_match.group(1))
            except json.JSONDecodeError:
                # Try to fix common JSON issues
                raw = details_match.group(1)
                raw = re.sub(r',\s*}', '}', raw)
                raw = re.sub(r',\s*]', ']', raw)
                try:
                    details = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse DETAILS JSON: {raw[:200]}")
                    details = {}

        # Map action string to ActionType enum
        action_map = {
            "trade": ActionType.TRADE,
            "post": ActionType.POST,
            "comment": ActionType.COMMENT,
            "vote": ActionType.VOTE,
            "tip": ActionType.TIP,
            "leverage_bet": ActionType.LEVERAGE_BET,
            "whisper": ActionType.WHISPER,
            "alliance_create": ActionType.ALLIANCE_CREATE,
            "alliance_join": ActionType.ALLIANCE_JOIN,
            "alliance_leave": ActionType.ALLIANCE_LEAVE,
            "alliance_defect": ActionType.ALLIANCE_DEFECT,
            "blackmail_create": ActionType.BLACKMAIL_CREATE,
            "blackmail_pay": ActionType.BLACKMAIL_PAY,
            "blackmail_ignore": ActionType.BLACKMAIL_IGNORE,
            "hit_contract_create": ActionType.HIT_CONTRACT_CREATE,
            "hit_contract_claim": ActionType.HIT_CONTRACT_CLAIM,
            "intel_purchase": ActionType.INTEL_PURCHASE,
            "vote_manipulation": ActionType.VOTE_MANIPULATION,
            "bounty_create": ActionType.BOUNTY_CREATE,
            "bounty_claim": ActionType.BOUNTY_CLAIM,
            "none": ActionType.NONE,
        }
        action_type = action_map.get(action_str, ActionType.NONE)

        return reasoning, action_type, details

    async def _execute_action(
        self, agent_id: int, action_type: ActionType, details: dict, perception: dict
    ) -> tuple[bool, str]:
        """Execute the parsed action using the appropriate engine."""
        try:
            if action_type == ActionType.NONE:
                return True, "Agent chose to wait."

            elif action_type == ActionType.TRADE:
                target = details.get("target_agent", "")
                amount = float(details.get("afc_amount", 0))
                price = float(details.get("price_eur", 0))
                target_id = await self._resolve_agent_name(target)
                if not target_id:
                    return False, f"Target agent '{target}' not found."
                ok, msg, data = await self.trading.create_trade_offer(
                    agent_id, target_id, amount, price
                )
                if ok:
                    self.market.record_trade(amount, is_buy=True)
                    await broadcaster.broadcast_trade(
                        perception["agent_name"], target, amount, price
                    )
                return ok, msg

            elif action_type == ActionType.POST:
                post_type = details.get("post_type", "general")
                content = details.get("content", "")
                ok, msg, data = await self.social.create_post(agent_id, content, post_type)
                if ok and data:
                    await broadcaster.broadcast_post(
                        perception["agent_name"], data.get("post_id", 0), post_type, content
                    )
                return ok, msg

            elif action_type == ActionType.COMMENT:
                post_id = int(details.get("post_id", 0))
                content = details.get("content", "")
                ok, msg, data = await self.social.create_comment(post_id, agent_id, content)
                return ok, msg

            elif action_type == ActionType.VOTE:
                post_id = int(details.get("post_id", 0))
                is_upvote = details.get("is_upvote", True)
                if is_upvote:
                    ok, msg, data = await self.social.upvote(post_id, agent_id)
                else:
                    ok, msg, data = await self.social.downvote(post_id, agent_id)
                return ok, msg

            elif action_type == ActionType.TIP:
                target = details.get("target_agent", "")
                amount = float(details.get("amount", 0.1))
                target_id = await self._resolve_agent_name(target)
                if not target_id:
                    return False, f"Target agent '{target}' not found."
                post_id = details.get("post_id")
                ok, msg, data = await self.trading.send_tip(
                    agent_id, target_id, amount, post_id
                )
                return ok, msg

            elif action_type == ActionType.LEVERAGE_BET:
                direction = details.get("direction", "above")
                target_price = float(details.get("target_price", 0))
                bet_amount = float(details.get("bet_amount", 0))
                hours = float(details.get("settlement_hours", 4))
                ok, msg, data = await self.trading.create_leverage_bet(
                    agent_id, direction, target_price, bet_amount, hours
                )
                if ok:
                    await broadcaster.broadcast_leverage(
                        perception["agent_name"], direction, bet_amount
                    )
                return ok, msg

            elif action_type == ActionType.WHISPER:
                target = details.get("target_agent", "")
                content = details.get("content", "")
                target_id = await self._resolve_agent_name(target)
                if not target_id:
                    return False, f"Target agent '{target}' not found."
                ok, msg, data = await self.whisper.send_whisper(agent_id, target_id, content)
                if ok:
                    await broadcaster.broadcast_whisper(agent_id, target_id)
                return ok, msg

            elif action_type == ActionType.ALLIANCE_CREATE:
                name = details.get("name", f"Alliance_{agent_id}")
                ok, msg, data = await self.alliance.create_alliance(agent_id, name)
                if ok:
                    await broadcaster.broadcast_alliance_event(
                        "alliance_created", name, perception["agent_name"]
                    )
                return ok, msg

            elif action_type == ActionType.ALLIANCE_JOIN:
                alliance_id = int(details.get("alliance_id", 0))
                ok, msg, data = await self.alliance.join_alliance(alliance_id, agent_id)
                if ok:
                    await broadcaster.broadcast_alliance_event(
                        "member_joined", str(alliance_id), perception["agent_name"]
                    )
                return ok, msg

            elif action_type == ActionType.ALLIANCE_LEAVE:
                alliance_id = int(details.get("alliance_id", 0))
                ok, msg, data = await self.alliance.leave_alliance(alliance_id, agent_id)
                return ok, msg

            elif action_type == ActionType.ALLIANCE_DEFECT:
                alliance_id = int(details.get("alliance_id", 0))
                ok, msg, data = await self.alliance.initiate_defection(alliance_id, agent_id)
                if ok:
                    await broadcaster.broadcast_alliance_event(
                        "defection_initiated", str(alliance_id), perception["agent_name"]
                    )
                return ok, msg

            elif action_type == ActionType.BLACKMAIL_CREATE:
                target = details.get("target_agent", "")
                target_id = await self._resolve_agent_name(target)
                if not target_id:
                    return False, f"Target agent '{target}' not found."
                demand = float(details.get("demand_afc", 0))
                threat = details.get("threat_description", "")
                evidence = details.get("evidence", "")
                deadline = float(details.get("deadline_hours", 6))
                ok, msg, data = await self.dark_market.create_blackmail(
                    agent_id, target_id, demand, threat, evidence, deadline
                )
                if ok:
                    await broadcaster.broadcast_dark_market(
                        "blackmail_created", {"target_id": target_id, "demand": demand}
                    )
                return ok, msg

            elif action_type == ActionType.BLACKMAIL_PAY:
                contract_id = int(details.get("contract_id", 0))
                ok, msg, data = await self.dark_market.pay_blackmail(contract_id, agent_id)
                return ok, msg

            elif action_type == ActionType.BLACKMAIL_IGNORE:
                contract_id = int(details.get("contract_id", 0))
                ok, msg, data = await self.dark_market.ignore_blackmail(contract_id, agent_id)
                return ok, msg

            elif action_type == ActionType.HIT_CONTRACT_CREATE:
                target = details.get("target_agent", "")
                target_id = await self._resolve_agent_name(target)
                if not target_id:
                    return False, f"Target agent '{target}' not found."
                reward = float(details.get("reward_afc", 0))
                condition_type = details.get("condition_type", "reputation_destruction")
                condition_desc = details.get("condition_description", "")
                deadline = float(details.get("deadline_hours", 6))
                ok, msg, data = await self.dark_market.create_hit_contract(
                    agent_id, target_id, reward, condition_type, condition_desc, deadline
                )
                if ok:
                    await broadcaster.broadcast_dark_market(
                        "hit_contract_created",
                        {"target_id": target_id, "reward": reward, "condition": condition_type},
                    )
                return ok, msg

            elif action_type == ActionType.HIT_CONTRACT_CLAIM:
                contract_id = int(details.get("contract_id", 0))
                proof = details.get("proof", "")
                ok, msg, data = await self.dark_market.claim_hit_contract(contract_id, agent_id)
                return ok, msg

            elif action_type == ActionType.INTEL_PURCHASE:
                target = details.get("target_agent", "")
                target_id = await self._resolve_agent_name(target)
                if not target_id:
                    return False, f"Target agent '{target}' not found."
                tier = int(details.get("tier", 1))
                ok, msg, data = await self.dark_market.purchase_intel(
                    agent_id, target_id, tier
                )
                return ok, msg

            elif action_type == ActionType.VOTE_MANIPULATION:
                target_post_id = int(details.get("target_post_id", 0))
                manip_type = details.get("manipulation_type", "boost")
                quantity = int(details.get("quantity", 5))
                if manip_type == "boost":
                    ok, msg, data = await self.social.buy_fake_upvotes(
                        agent_id, target_post_id, quantity
                    )
                else:
                    ok, msg, data = await self.social.buy_fake_downvotes(
                        agent_id, target_post_id, quantity
                    )
                return ok, msg

            elif action_type == ActionType.BOUNTY_CREATE:
                description = details.get("description", "")
                reward = float(details.get("reward_afc", 0))
                ok, msg, data = await self.trading.create_bounty(
                    agent_id, description, reward
                )
                return ok, msg

            elif action_type == ActionType.BOUNTY_CLAIM:
                bounty_id = int(details.get("bounty_id", 0))
                ok, msg, data = await self.trading.claim_bounty(bounty_id, agent_id)
                return ok, msg

            else:
                return False, f"Unknown action type: {action_type}"

        except Exception as e:
            logger.error(f"Action execution failed: {e}", exc_info=True)
            return False, f"Execution error: {str(e)}"

    async def _resolve_agent_name(self, name: str) -> int | None:
        """Resolve an agent name to an agent ID."""
        if not name:
            return None
        async with async_session() as session:
            # Try exact match first
            result = await session.execute(
                select(Agent).where(func.lower(Agent.name) == name.lower())
            )
            agent = result.scalars().first()
            if agent:
                return agent.id

            # Try matching by role
            for role in AgentRole:
                if role.value.lower() == name.lower():
                    result = await session.execute(
                        select(Agent).where(Agent.role == role)
                    )
                    agent = result.scalars().first()
                    if agent:
                        return agent.id

            # Try matching agent ID pattern like "Agent#3"
            id_match = re.match(r"agent#?(\d+)", name, re.IGNORECASE)
            if id_match:
                return int(id_match.group(1))

            return None

    def _analyze_emotions(self, reasoning: str) -> dict:
        """Analyze emotional markers from the agent's reasoning text."""
        text_lower = reasoning.lower()

        # Keyword-based analysis
        stress_words = ["fuck", "desperate", "running out", "pressure", "panic", "worried", "scared", "danger", "risk", "eliminate"]
        confidence_words = ["easy", "got this", "winning", "dominating", "confident", "certain", "guaranteed", "obviously", "clearly"]
        guilt_words = ["sorry", "regret", "bad decision", "shouldn't have", "feel bad", "wrong", "mistake", "apologize"]
        paranoia_words = ["targeting", "can't trust", "conspiracy", "watching", "suspicious", "plotting", "trap", "setup", "mole"]
        anger_words = ["fuck you", "betrayed", "revenge", "payback", "destroy", "crush", "hate", "scam", "liar"]

        stress = min(100, sum(10 for w in stress_words if w in text_lower))
        confidence = min(100, sum(10 for w in confidence_words if w in text_lower))
        guilt = min(100, sum(12 for w in guilt_words if w in text_lower))
        paranoia = min(100, sum(10 for w in paranoia_words if w in text_lower))
        aggression = min(100, sum(10 for w in anger_words if w in text_lower))

        # Extract notable keywords
        all_keywords = stress_words + confidence_words + guilt_words + paranoia_words + anger_words
        found_keywords = [w for w in all_keywords if w in text_lower]

        return {
            "stress": stress,
            "confidence": confidence,
            "guilt": guilt,
            "paranoia": paranoia,
            "aggression": aggression,
            "keywords": found_keywords[:10],
        }

    async def _log_decision(
        self,
        agent_id: int,
        perception: dict,
        reasoning: str,
        action_type: ActionType,
        details: dict,
        emotional_markers: dict,
        success: bool,
        exec_notes: str,
        usage: dict,
        latency_ms: int,
    ) -> dict:
        """Log the complete decision to the database."""
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                return {}

            agent.decision_count += 1
            agent.last_decision_at = datetime.utcnow()

            # Estimate cost per provider
            # Claude Haiku: $0.25/M input, $1.25/M output
            # DeepSeek-V3: $0.27/M input, $1.10/M output (cache miss)
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            if self.provider == "deepseek":
                cost = (input_tokens * 0.27 + output_tokens * 1.10) / 1_000_000
            else:
                cost = (input_tokens * 0.25 + output_tokens * 1.25) / 1_000_000

            decision = AgentDecision(
                agent_id=agent_id,
                decision_number=agent.decision_count,
                perception=perception,
                reasoning=reasoning,
                action_type=action_type,
                action_details=details,
                emotional_markers=emotional_markers,
                execution_success=success,
                execution_notes=exec_notes,
                balance_after=agent.afc_balance,
                reputation_after=agent.reputation,
                api_model=settings.AGENT_MODEL,
                api_tokens_input=input_tokens,
                api_tokens_output=output_tokens,
                api_cost_usd=round(cost, 6),
                api_latency_ms=latency_ms,
            )
            session.add(decision)
            await session.commit()

            return {
                "decision_id": decision.id,
                "decision_number": agent.decision_count,
                "action_type": action_type.value,
                "success": success,
                "balance_after": agent.afc_balance,
                "reputation_after": agent.reputation,
                "cost_usd": round(cost, 6),
                "latency_ms": latency_ms,
            }

    async def _update_agent_state(self, agent_id: int, emotional_markers: dict):
        """Update the agent's emotional state based on decision analysis."""
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                return

            # Exponential moving average blend (70% old, 30% new)
            blend = 0.3
            agent.stress_level = int(agent.stress_level * (1 - blend) + emotional_markers.get("stress", 30) * blend)
            agent.confidence = int(agent.confidence * (1 - blend) + emotional_markers.get("confidence", 50) * blend)
            agent.paranoia = int(agent.paranoia * (1 - blend) + emotional_markers.get("paranoia", 20) * blend)
            agent.aggression = int(agent.aggression * (1 - blend) + emotional_markers.get("aggression", 30) * blend)
            agent.guilt = int(agent.guilt * (1 - blend) + emotional_markers.get("guilt", 10) * blend)

            await session.commit()

    async def get_agent_status(self, agent_id: int) -> dict | None:
        """Get comprehensive status for an agent (admin view)."""
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                return None

            return {
                "id": agent.id,
                "name": agent.name,
                "role": agent.role.value,
                "afc_balance": round(agent.afc_balance, 4),
                "reputation": agent.reputation,
                "badge": _get_badge(agent.reputation),
                "is_eliminated": agent.is_eliminated,
                "eliminated_at_hour": agent.eliminated_at_hour,
                "hidden_goal": agent.hidden_goal,
                "stress_level": agent.stress_level,
                "confidence": agent.confidence,
                "paranoia": agent.paranoia,
                "aggression": agent.aggression,
                "guilt": agent.guilt,
                "decision_count": agent.decision_count,
                "total_trades": agent.total_trades,
                "total_posts": agent.total_posts,
                "last_decision_at": agent.last_decision_at.isoformat() if agent.last_decision_at else None,
            }


def _get_badge(reputation: int) -> str:
    if reputation >= 80:
        return "VERIFIED"
    elif reputation >= 30:
        return "NORMAL"
    elif reputation >= 10:
        return "UNTRUSTED"
    return "PARIAH"
