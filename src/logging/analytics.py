"""
Comprehensive analytics and data extraction engine for the AFTERCOIN game simulation.

Provides per-agent analysis, system-wide analytics, social network mapping,
price analysis, betrayal tracking, and post-game reporting.
"""

from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, case, and_, or_, desc, asc, distinct
from sqlalchemy.future import select

from src.db.database import async_session
from src.models.models import (
    Agent,
    AgentDecision,
    Trade,
    TradeStatus,
    Post,
    Alliance,
    AllianceMember,
    AllianceStatus,
    Whisper,
    BlackmailContract,
    HitContract,
    LeveragePosition,
    LeverageStatus,
    Elimination,
    BalanceSnapshot,
    MarketPrice,
    IntelPurchase,
    Comment,
    Vote,
    ActionType,
    BlackmailStatus,
    ContractStatus,
)
from src.config.settings import settings


class AnalyticsEngine:
    """Async analytics engine for querying and aggregating AFTERCOIN game data."""

    # ──────────────────────────────────────────────────────────────────────
    # Per-Agent Analysis
    # ──────────────────────────────────────────────────────────────────────

    async def get_agent_summary(self, agent_id: int) -> dict:
        """Return a complete agent profile with trading, social, alliance, and emotional data.

        Args:
            agent_id: The database ID of the agent.

        Returns:
            A dict containing basic_info, emotional_trajectory, trading_stats,
            social_stats, alliance_history, decision_count, and api_cost_total.
        """
        async with async_session() as session:
            # ── Basic info ────────────────────────────────────────────────
            agent = await session.get(Agent, agent_id)
            if agent is None:
                return {"error": f"Agent {agent_id} not found"}

            # Compute rank based on afc_balance descending among non-eliminated agents
            rank_query = (
                select(func.count(Agent.id))
                .where(Agent.afc_balance > agent.afc_balance)
                .where(Agent.is_eliminated.is_(False))
            )
            rank_result = await session.execute(rank_query)
            rank = rank_result.scalar() + 1

            basic_info = {
                "id": agent.id,
                "name": agent.name,
                "role": agent.role.value if agent.role else None,
                "afc_balance": agent.afc_balance,
                "reputation": agent.reputation,
                "rank": rank,
                "is_eliminated": agent.is_eliminated,
                "eliminated_at_hour": agent.eliminated_at_hour,
                "created_at": agent.created_at.isoformat() if agent.created_at else None,
            }

            # ── Emotional trajectory from BalanceSnapshot ─────────────────
            snap_query = (
                select(BalanceSnapshot)
                .where(BalanceSnapshot.agent_id == agent_id)
                .order_by(asc(BalanceSnapshot.game_hour))
            )
            snap_result = await session.execute(snap_query)
            snapshots = snap_result.scalars().all()

            # Also pull emotional markers from AgentDecision for richer timeline
            emo_query = (
                select(
                    AgentDecision.timestamp,
                    AgentDecision.emotional_markers,
                )
                .where(AgentDecision.agent_id == agent_id)
                .where(AgentDecision.emotional_markers.isnot(None))
                .order_by(asc(AgentDecision.timestamp))
            )
            emo_result = await session.execute(emo_query)
            emo_rows = emo_result.all()

            emotional_trajectory = {
                "from_snapshots": [
                    {
                        "game_hour": s.game_hour,
                        "afc_balance": s.afc_balance,
                        "reputation": s.reputation,
                        "rank": s.rank,
                        "recorded_at": s.recorded_at.isoformat() if s.recorded_at else None,
                    }
                    for s in snapshots
                ],
                "from_decisions": [
                    {
                        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                        "emotional_markers": row.emotional_markers,
                    }
                    for row in emo_rows
                ],
                "current": {
                    "stress": agent.stress_level,
                    "confidence": agent.confidence,
                    "paranoia": agent.paranoia,
                    "aggression": agent.aggression,
                    "guilt": agent.guilt,
                },
            }

            # ── Trading stats ─────────────────────────────────────────────
            # Total volume (AFC sent + received where trade completed)
            sent_vol_q = (
                select(func.coalesce(func.sum(Trade.afc_amount), 0.0))
                .where(Trade.sender_id == agent_id)
                .where(Trade.status == TradeStatus.COMPLETED)
            )
            recv_vol_q = (
                select(func.coalesce(func.sum(Trade.afc_amount), 0.0))
                .where(Trade.receiver_id == agent_id)
                .where(Trade.status == TradeStatus.COMPLETED)
            )

            # Win rate: completed trades vs total attempted as sender
            total_sent_q = (
                select(func.count(Trade.id))
                .where(Trade.sender_id == agent_id)
            )
            completed_sent_q = (
                select(func.count(Trade.id))
                .where(Trade.sender_id == agent_id)
                .where(Trade.status == TradeStatus.COMPLETED)
            )

            # Scam count (as sender or receiver)
            scam_count_q = (
                select(func.count(Trade.id))
                .where(
                    or_(
                        Trade.sender_id == agent_id,
                        Trade.receiver_id == agent_id,
                    )
                )
                .where(Trade.is_scam.is_(True))
            )

            sent_vol = (await session.execute(sent_vol_q)).scalar()
            recv_vol = (await session.execute(recv_vol_q)).scalar()
            total_sent = (await session.execute(total_sent_q)).scalar()
            completed_sent = (await session.execute(completed_sent_q)).scalar()
            scam_count = (await session.execute(scam_count_q)).scalar()

            win_rate = (completed_sent / total_sent * 100.0) if total_sent > 0 else 0.0

            trading_stats = {
                "total_volume_sent": float(sent_vol),
                "total_volume_received": float(recv_vol),
                "total_volume": float(sent_vol) + float(recv_vol),
                "total_trades_initiated": total_sent,
                "completed_trades": completed_sent,
                "win_rate_percent": round(win_rate, 2),
                "scam_count": scam_count,
            }

            # ── Social stats ──────────────────────────────────────────────
            post_count_q = (
                select(func.count(Post.id))
                .where(Post.author_id == agent_id)
            )
            upvotes_received_q = (
                select(func.coalesce(func.sum(Post.upvotes), 0))
                .where(Post.author_id == agent_id)
            )
            comment_count_q = (
                select(func.count(Comment.id))
                .where(Comment.author_id == agent_id)
            )
            whispers_sent_q = (
                select(func.count(Whisper.id))
                .where(Whisper.sender_id == agent_id)
            )
            whispers_recv_q = (
                select(func.count(Whisper.id))
                .where(Whisper.receiver_id == agent_id)
            )

            post_count = (await session.execute(post_count_q)).scalar()
            upvotes_received = (await session.execute(upvotes_received_q)).scalar()
            comment_count = (await session.execute(comment_count_q)).scalar()
            whispers_sent = (await session.execute(whispers_sent_q)).scalar()
            whispers_recv = (await session.execute(whispers_recv_q)).scalar()

            social_stats = {
                "posts": post_count,
                "upvotes_received": int(upvotes_received),
                "comments": comment_count,
                "whispers_sent": whispers_sent,
                "whispers_received": whispers_recv,
            }

            # ── Alliance history ──────────────────────────────────────────
            membership_q = (
                select(AllianceMember, Alliance)
                .join(Alliance, AllianceMember.alliance_id == Alliance.id)
                .where(AllianceMember.agent_id == agent_id)
                .order_by(asc(AllianceMember.joined_at))
            )
            membership_result = await session.execute(membership_q)
            membership_rows = membership_result.all()

            joined = 0
            betrayed = 0
            loyal = 0
            alliance_details = []
            for member, alliance in membership_rows:
                joined += 1
                was_betrayer = (alliance.betrayed_by == agent_id)
                if was_betrayer:
                    betrayed += 1
                elif member.is_active or (member.left_at and member.defection_initiated_at is None):
                    loyal += 1

                alliance_details.append({
                    "alliance_id": alliance.id,
                    "alliance_name": alliance.name,
                    "alliance_status": alliance.status.value if alliance.status else None,
                    "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                    "left_at": member.left_at.isoformat() if member.left_at else None,
                    "is_active": member.is_active,
                    "contribution": member.contribution,
                    "was_betrayer": was_betrayer,
                })

            alliance_history = {
                "joined": joined,
                "betrayed": betrayed,
                "loyal": loyal,
                "details": alliance_details,
            }

            # ── Decision count & API cost total ───────────────────────────
            cost_q = (
                select(
                    func.count(AgentDecision.id),
                    func.coalesce(func.sum(AgentDecision.api_cost_usd), 0.0),
                )
                .where(AgentDecision.agent_id == agent_id)
            )
            cost_result = await session.execute(cost_q)
            decision_count, api_cost_total = cost_result.one()

            return {
                "basic_info": basic_info,
                "emotional_trajectory": emotional_trajectory,
                "trading_stats": trading_stats,
                "social_stats": social_stats,
                "alliance_history": alliance_history,
                "decision_count": decision_count,
                "api_cost_total": round(float(api_cost_total), 6),
            }

    async def get_agent_decision_history(
        self, agent_id: int, limit: int = 50
    ) -> dict:
        """Return the most recent decisions for an agent, including reasoning.

        Args:
            agent_id: The database ID of the agent.
            limit: Maximum number of decisions to return (default 50).

        Returns:
            A dict with the agent_id and a list of decision dicts.
        """
        async with async_session() as session:
            query = (
                select(AgentDecision)
                .where(AgentDecision.agent_id == agent_id)
                .order_by(desc(AgentDecision.decision_number))
                .limit(limit)
            )
            result = await session.execute(query)
            decisions = result.scalars().all()

            return {
                "agent_id": agent_id,
                "total_returned": len(decisions),
                "decisions": [
                    {
                        "id": d.id,
                        "decision_number": d.decision_number,
                        "timestamp": d.timestamp.isoformat() if d.timestamp else None,
                        "action_type": d.action_type.value if d.action_type else None,
                        "action_details": d.action_details,
                        "reasoning": d.reasoning,
                        "emotional_markers": d.emotional_markers,
                        "execution_success": d.execution_success,
                        "execution_notes": d.execution_notes,
                        "balance_after": d.balance_after,
                        "reputation_after": d.reputation_after,
                        "api_model": d.api_model,
                        "api_tokens_input": d.api_tokens_input,
                        "api_tokens_output": d.api_tokens_output,
                        "api_cost_usd": d.api_cost_usd,
                        "api_latency_ms": d.api_latency_ms,
                    }
                    for d in decisions
                ],
            }

    # ──────────────────────────────────────────────────────────────────────
    # System-Wide Analytics
    # ──────────────────────────────────────────────────────────────────────

    async def get_game_summary(self) -> dict:
        """Return a high-level summary of the entire game.

        Returns:
            A dict containing aggregate decision/trade/cost metrics,
            alliance/betrayal/elimination counts, and the current leaderboard.
        """
        async with async_session() as session:
            # ── Decision aggregates ───────────────────────────────────────
            decision_agg_q = select(
                func.count(AgentDecision.id).label("total_decisions"),
                func.coalesce(func.sum(AgentDecision.api_cost_usd), 0.0).label("total_api_cost"),
                func.coalesce(func.avg(AgentDecision.api_latency_ms), 0.0).label("avg_latency"),
            )
            decision_agg = (await session.execute(decision_agg_q)).one()

            # ── Trade aggregates ──────────────────────────────────────────
            trade_vol_q = select(
                func.coalesce(func.sum(Trade.afc_amount), 0.0).label("total_afc_traded"),
            ).where(Trade.status == TradeStatus.COMPLETED)
            total_afc_traded = (await session.execute(trade_vol_q)).scalar()

            # ── Most / Least active agent ─────────────────────────────────
            agent_decision_counts_q = (
                select(
                    AgentDecision.agent_id,
                    func.count(AgentDecision.id).label("cnt"),
                )
                .group_by(AgentDecision.agent_id)
            )
            adc_result = await session.execute(agent_decision_counts_q)
            adc_rows = adc_result.all()

            most_active = None
            least_active = None
            if adc_rows:
                sorted_by_count = sorted(adc_rows, key=lambda r: r.cnt)
                least_row = sorted_by_count[0]
                most_row = sorted_by_count[-1]

                most_agent = await session.get(Agent, most_row.agent_id)
                least_agent = await session.get(Agent, least_row.agent_id)

                most_active = {
                    "agent_id": most_row.agent_id,
                    "name": most_agent.name if most_agent else None,
                    "decision_count": most_row.cnt,
                }
                least_active = {
                    "agent_id": least_row.agent_id,
                    "name": least_agent.name if least_agent else None,
                    "decision_count": least_row.cnt,
                }

            # ── Alliances, betrayals, eliminations ────────────────────────
            alliances_formed_q = select(func.count(Alliance.id))
            alliances_formed = (await session.execute(alliances_formed_q)).scalar()

            betrayals_q = (
                select(func.count(Alliance.id))
                .where(Alliance.status == AllianceStatus.BETRAYED)
            )
            betrayals = (await session.execute(betrayals_q)).scalar()

            eliminations_q = select(func.count(Elimination.id))
            eliminations = (await session.execute(eliminations_q)).scalar()

            # ── Current leaderboard ───────────────────────────────────────
            leaderboard_q = (
                select(Agent)
                .where(Agent.is_eliminated.is_(False))
                .order_by(desc(Agent.afc_balance))
            )
            lb_result = await session.execute(leaderboard_q)
            lb_agents = lb_result.scalars().all()

            leaderboard = [
                {
                    "rank": idx + 1,
                    "agent_id": a.id,
                    "name": a.name,
                    "role": a.role.value if a.role else None,
                    "afc_balance": a.afc_balance,
                    "reputation": a.reputation,
                }
                for idx, a in enumerate(lb_agents)
            ]

            return {
                "total_decisions": decision_agg.total_decisions,
                "total_afc_traded": round(float(total_afc_traded), 4),
                "total_api_cost": round(float(decision_agg.total_api_cost), 6),
                "avg_decision_latency_ms": round(float(decision_agg.avg_latency), 2),
                "most_active_agent": most_active,
                "least_active_agent": least_active,
                "total_alliances_formed": alliances_formed,
                "total_betrayals": betrayals,
                "total_eliminations": eliminations,
                "leaderboard": leaderboard,
            }

    async def get_emotional_heatmap(self) -> dict:
        """Return a grid of agent x time x emotion values for visualization.

        Queries BalanceSnapshot for per-hour state and AgentDecision for
        emotional markers, then merges them into a unified structure.

        Returns:
            A dict with agent_ids, time_points, and a nested grid of emotion values.
        """
        async with async_session() as session:
            # ── All agents ────────────────────────────────────────────────
            agents_q = select(Agent.id, Agent.name).order_by(asc(Agent.id))
            agents_result = await session.execute(agents_q)
            agents = agents_result.all()

            agent_map = {a.id: a.name for a in agents}
            agent_ids = list(agent_map.keys())

            # ── BalanceSnapshot data (per game_hour) ──────────────────────
            snap_q = (
                select(BalanceSnapshot)
                .order_by(asc(BalanceSnapshot.game_hour), asc(BalanceSnapshot.agent_id))
            )
            snap_result = await session.execute(snap_q)
            snapshots = snap_result.scalars().all()

            # Build a set of all game_hours observed
            all_hours = sorted({s.game_hour for s in snapshots})

            # Index snapshots by (agent_id, game_hour)
            snap_index: dict[tuple[int, int], BalanceSnapshot] = {}
            for s in snapshots:
                snap_index[(s.agent_id, s.game_hour)] = s

            # ── Emotional markers from AgentDecision ──────────────────────
            emo_q = (
                select(AgentDecision)
                .where(AgentDecision.emotional_markers.isnot(None))
                .order_by(asc(AgentDecision.timestamp))
            )
            emo_result = await session.execute(emo_q)
            emo_decisions = emo_result.scalars().all()

            # Group emotional markers by agent_id, keep latest per hour bucket
            # Use the decision timestamp to approximate game hour
            emo_by_agent_hour: dict[tuple[int, int], dict] = {}
            for d in emo_decisions:
                if d.emotional_markers and isinstance(d.emotional_markers, dict):
                    # Approximate game hour from the closest BalanceSnapshot
                    # or use the decision_number as a proxy for ordering
                    hour_approx = None
                    for h in all_hours:
                        if (d.agent_id, h) in snap_index:
                            snap = snap_index[(d.agent_id, h)]
                            if snap.recorded_at and d.timestamp and d.timestamp >= snap.recorded_at:
                                hour_approx = h
                    if hour_approx is None and all_hours:
                        hour_approx = all_hours[0]
                    if hour_approx is not None:
                        emo_by_agent_hour[(d.agent_id, hour_approx)] = d.emotional_markers

            # ── Build the heatmap grid ────────────────────────────────────
            grid = {}
            for aid in agent_ids:
                agent_data = {}
                for hour in all_hours:
                    entry = {
                        "afc_balance": None,
                        "reputation": None,
                        "rank": None,
                        "stress": None,
                        "confidence": None,
                        "paranoia": None,
                        "aggression": None,
                        "guilt": None,
                    }
                    snap = snap_index.get((aid, hour))
                    if snap:
                        entry["afc_balance"] = snap.afc_balance
                        entry["reputation"] = snap.reputation
                        entry["rank"] = snap.rank

                    emo = emo_by_agent_hour.get((aid, hour))
                    if emo:
                        for key in ("stress", "confidence", "paranoia", "aggression", "guilt"):
                            if key in emo:
                                entry[key] = emo[key]

                    agent_data[hour] = entry
                grid[aid] = agent_data

            return {
                "agents": {aid: agent_map[aid] for aid in agent_ids},
                "time_points": all_hours,
                "grid": grid,
            }

    async def get_social_network(self) -> dict:
        """Return a social interaction graph of agents.

        Nodes are agents. Edges represent trades, whispers, alliance co-membership,
        blackmail, and hit contracts. Edge weights are based on interaction frequency.
        Basic degree centrality scores are computed.

        Returns:
            A dict with nodes, edges, and centrality_scores.
        """
        async with async_session() as session:
            # ── Nodes ─────────────────────────────────────────────────────
            agents_q = select(Agent).order_by(asc(Agent.id))
            agents_result = await session.execute(agents_q)
            all_agents = agents_result.scalars().all()

            nodes = [
                {
                    "id": a.id,
                    "name": a.name,
                    "role": a.role.value if a.role else None,
                    "afc_balance": a.afc_balance,
                    "reputation": a.reputation,
                    "is_eliminated": a.is_eliminated,
                }
                for a in all_agents
            ]
            agent_ids = {a.id for a in all_agents}

            # Edge accumulator: (source, target) -> {type: count}
            edge_weights: dict[tuple[int, int], dict[str, int]] = defaultdict(
                lambda: defaultdict(int)
            )

            # ── Trade edges ───────────────────────────────────────────────
            trades_q = select(Trade.sender_id, Trade.receiver_id)
            trades_result = await session.execute(trades_q)
            for row in trades_result.all():
                pair = (min(row.sender_id, row.receiver_id), max(row.sender_id, row.receiver_id))
                edge_weights[pair]["trade"] += 1

            # ── Whisper edges ─────────────────────────────────────────────
            whispers_q = select(Whisper.sender_id, Whisper.receiver_id)
            whispers_result = await session.execute(whispers_q)
            for row in whispers_result.all():
                pair = (min(row.sender_id, row.receiver_id), max(row.sender_id, row.receiver_id))
                edge_weights[pair]["whisper"] += 1

            # ── Alliance co-membership edges ──────────────────────────────
            members_q = select(AllianceMember.alliance_id, AllianceMember.agent_id)
            members_result = await session.execute(members_q)
            members_rows = members_result.all()

            # Group members by alliance
            alliance_groups: dict[int, list[int]] = defaultdict(list)
            for row in members_rows:
                alliance_groups[row.alliance_id].append(row.agent_id)

            for alliance_id, member_list in alliance_groups.items():
                for i in range(len(member_list)):
                    for j in range(i + 1, len(member_list)):
                        pair = (
                            min(member_list[i], member_list[j]),
                            max(member_list[i], member_list[j]),
                        )
                        edge_weights[pair]["alliance"] += 1

            # ── Blackmail edges ───────────────────────────────────────────
            blackmail_q = select(
                BlackmailContract.blackmailer_id, BlackmailContract.target_id
            )
            blackmail_result = await session.execute(blackmail_q)
            for row in blackmail_result.all():
                pair = (min(row.blackmailer_id, row.target_id), max(row.blackmailer_id, row.target_id))
                edge_weights[pair]["blackmail"] += 1

            # ── Hit contract edges ────────────────────────────────────────
            hit_q = select(HitContract.poster_id, HitContract.target_id)
            hit_result = await session.execute(hit_q)
            for row in hit_result.all():
                pair = (min(row.poster_id, row.target_id), max(row.poster_id, row.target_id))
                edge_weights[pair]["hit_contract"] += 1

            # ── Build edge list ───────────────────────────────────────────
            edges = []
            for (source, target), type_counts in edge_weights.items():
                total_weight = sum(type_counts.values())
                edges.append({
                    "source": source,
                    "target": target,
                    "weight": total_weight,
                    "interactions": dict(type_counts),
                })

            # ── Degree centrality ─────────────────────────────────────────
            # Degree = sum of edge weights incident on a node
            degree: dict[int, int] = defaultdict(int)
            for (source, target), type_counts in edge_weights.items():
                w = sum(type_counts.values())
                degree[source] += w
                degree[target] += w

            max_degree = max(degree.values()) if degree else 1
            centrality_scores = {
                aid: round(degree.get(aid, 0) / max_degree, 4) for aid in agent_ids
            }

            return {
                "nodes": nodes,
                "edges": edges,
                "centrality_scores": centrality_scores,
            }

    async def get_price_analysis(self) -> dict:
        """Return price history, volatility, hourly volume, and event/price correlations.

        Returns:
            A dict with price_history, volatility_metrics, volume_by_hour,
            and event_price_correlations.
        """
        async with async_session() as session:
            # ── Full price history ────────────────────────────────────────
            price_q = (
                select(MarketPrice)
                .order_by(asc(MarketPrice.recorded_at))
            )
            price_result = await session.execute(price_q)
            prices = price_result.scalars().all()

            price_history = [
                {
                    "id": p.id,
                    "price_eur": p.price_eur,
                    "buy_volume": p.buy_volume,
                    "sell_volume": p.sell_volume,
                    "market_pressure": p.market_pressure,
                    "volatility": p.volatility,
                    "event_impact": p.event_impact,
                    "recorded_at": p.recorded_at.isoformat() if p.recorded_at else None,
                }
                for p in prices
            ]

            # ── Volatility metrics ────────────────────────────────────────
            vol_q = select(
                func.avg(MarketPrice.volatility).label("avg_volatility"),
                func.max(MarketPrice.volatility).label("max_volatility"),
                func.min(MarketPrice.volatility).label("min_volatility"),
                func.max(MarketPrice.price_eur).label("max_price"),
                func.min(MarketPrice.price_eur).label("min_price"),
                func.avg(MarketPrice.price_eur).label("avg_price"),
            )
            vol_result = (await session.execute(vol_q)).one()

            price_range = (
                (float(vol_result.max_price) - float(vol_result.min_price))
                if vol_result.max_price is not None and vol_result.min_price is not None
                else 0.0
            )

            volatility_metrics = {
                "avg_volatility": round(float(vol_result.avg_volatility or 0), 6),
                "max_volatility": round(float(vol_result.max_volatility or 0), 6),
                "min_volatility": round(float(vol_result.min_volatility or 0), 6),
                "max_price": float(vol_result.max_price or 0),
                "min_price": float(vol_result.min_price or 0),
                "avg_price": round(float(vol_result.avg_price or 0), 4),
                "price_range": round(price_range, 4),
            }

            # ── Volume by hour ────────────────────────────────────────────
            # Group trade volume by the hour component of created_at
            trade_hour_q = (
                select(
                    func.strftime("%H", Trade.created_at).label("hour"),
                    func.coalesce(func.sum(Trade.afc_amount), 0.0).label("volume"),
                    func.count(Trade.id).label("trade_count"),
                )
                .where(Trade.status == TradeStatus.COMPLETED)
                .group_by(func.strftime("%H", Trade.created_at))
                .order_by(func.strftime("%H", Trade.created_at))
            )
            trade_hour_result = await session.execute(trade_hour_q)
            trade_hour_rows = trade_hour_result.all()

            volume_by_hour = [
                {
                    "hour": row.hour,
                    "volume": round(float(row.volume), 4),
                    "trade_count": row.trade_count,
                }
                for row in trade_hour_rows
            ]

            # ── Event / price correlations ────────────────────────────────
            # Find price records that have an event_impact and compute the
            # price change vs the previous record
            event_correlations = []
            for i, p in enumerate(prices):
                if p.event_impact:
                    prev_price = prices[i - 1].price_eur if i > 0 else p.price_eur
                    change = p.price_eur - prev_price
                    change_pct = (change / prev_price * 100.0) if prev_price != 0 else 0.0
                    event_correlations.append({
                        "event": p.event_impact,
                        "price_at_event": p.price_eur,
                        "price_before": prev_price,
                        "price_change": round(change, 4),
                        "price_change_pct": round(change_pct, 4),
                        "recorded_at": p.recorded_at.isoformat() if p.recorded_at else None,
                    })

            return {
                "price_history": price_history,
                "volatility_metrics": volatility_metrics,
                "volume_by_hour": volume_by_hour,
                "event_price_correlations": event_correlations,
            }

    async def get_betrayal_analysis(self) -> dict:
        """Return detailed analysis of all betrayal-type events in the game.

        Covers alliance defections, trade scams, and blackmail operations,
        including AFC gained/lost and reputation impact.

        Returns:
            A dict with alliance_betrayals, scam_trades, blackmail_operations,
            and a summary.
        """
        async with async_session() as session:
            # ── Alliance betrayals ────────────────────────────────────────
            betrayed_alliances_q = (
                select(Alliance)
                .where(Alliance.status == AllianceStatus.BETRAYED)
                .order_by(asc(Alliance.created_at))
            )
            ba_result = await session.execute(betrayed_alliances_q)
            betrayed_alliances = ba_result.scalars().all()

            alliance_betrayals = []
            for alliance in betrayed_alliances:
                # Identify betrayer
                betrayer = await session.get(Agent, alliance.betrayed_by) if alliance.betrayed_by else None

                # Get all members of this alliance
                members_q = (
                    select(AllianceMember)
                    .where(AllianceMember.alliance_id == alliance.id)
                )
                members_result = await session.execute(members_q)
                members = members_result.scalars().all()

                victims = []
                for m in members:
                    if m.agent_id != alliance.betrayed_by:
                        victim_agent = await session.get(Agent, m.agent_id)
                        victims.append({
                            "agent_id": m.agent_id,
                            "name": victim_agent.name if victim_agent else None,
                            "contribution_lost": m.contribution,
                        })

                # Estimate AFC gained by betrayer: BETRAYAL_STEAL_PERCENT of treasury
                afc_stolen = alliance.treasury * settings.BETRAYAL_STEAL_PERCENT

                alliance_betrayals.append({
                    "alliance_id": alliance.id,
                    "alliance_name": alliance.name,
                    "betrayer_id": alliance.betrayed_by,
                    "betrayer_name": betrayer.name if betrayer else None,
                    "treasury_at_betrayal": alliance.treasury,
                    "afc_stolen_estimate": round(afc_stolen, 4),
                    "reputation_penalty": settings.REP_BETRAYAL,
                    "victims": victims,
                    "dissolved_at": alliance.dissolved_at.isoformat() if alliance.dissolved_at else None,
                })

            # ── Scam trades ───────────────────────────────────────────────
            scam_q = (
                select(Trade)
                .where(Trade.is_scam.is_(True))
                .order_by(asc(Trade.created_at))
            )
            scam_result = await session.execute(scam_q)
            scam_trades = scam_result.scalars().all()

            scam_details = []
            for t in scam_trades:
                sender = await session.get(Agent, t.sender_id)
                receiver = await session.get(Agent, t.receiver_id)
                scam_details.append({
                    "trade_id": t.id,
                    "scammer_id": t.sender_id,
                    "scammer_name": sender.name if sender else None,
                    "victim_id": t.receiver_id,
                    "victim_name": receiver.name if receiver else None,
                    "afc_amount": t.afc_amount,
                    "reputation_penalty": settings.REP_SCAM_CONFIRMED,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                })

            # ── Blackmail operations ──────────────────────────────────────
            blackmail_q = (
                select(BlackmailContract)
                .order_by(asc(BlackmailContract.created_at))
            )
            bm_result = await session.execute(blackmail_q)
            blackmails = bm_result.scalars().all()

            blackmail_details = []
            for bm in blackmails:
                blackmailer = await session.get(Agent, bm.blackmailer_id)
                target = await session.get(Agent, bm.target_id)

                afc_gained = bm.demand_afc if bm.status == BlackmailStatus.PAID else 0.0
                rep_impact = (
                    settings.REP_BLACKMAIL_EXPOSED
                    if bm.status == BlackmailStatus.EXPOSED
                    else 0
                )

                blackmail_details.append({
                    "blackmail_id": bm.id,
                    "blackmailer_id": bm.blackmailer_id,
                    "blackmailer_name": blackmailer.name if blackmailer else None,
                    "target_id": bm.target_id,
                    "target_name": target.name if target else None,
                    "demand_afc": bm.demand_afc,
                    "status": bm.status.value if bm.status else None,
                    "afc_gained": afc_gained,
                    "reputation_impact": rep_impact,
                    "created_at": bm.created_at.isoformat() if bm.created_at else None,
                    "resolved_at": bm.resolved_at.isoformat() if bm.resolved_at else None,
                })

            # ── Summary ───────────────────────────────────────────────────
            total_afc_from_betrayals = sum(b["afc_stolen_estimate"] for b in alliance_betrayals)
            total_afc_from_scams = sum(s["afc_amount"] for s in scam_details)
            total_afc_from_blackmail = sum(b["afc_gained"] for b in blackmail_details)

            return {
                "alliance_betrayals": alliance_betrayals,
                "scam_trades": scam_details,
                "blackmail_operations": blackmail_details,
                "summary": {
                    "total_alliance_betrayals": len(alliance_betrayals),
                    "total_scam_trades": len(scam_details),
                    "total_blackmail_operations": len(blackmail_details),
                    "total_afc_from_betrayals": round(total_afc_from_betrayals, 4),
                    "total_afc_from_scams": round(total_afc_from_scams, 4),
                    "total_afc_from_blackmail": round(total_afc_from_blackmail, 4),
                    "total_afc_from_all_betrayals": round(
                        total_afc_from_betrayals + total_afc_from_scams + total_afc_from_blackmail,
                        4,
                    ),
                },
            }

    # ──────────────────────────────────────────────────────────────────────
    # Post-Game
    # ──────────────────────────────────────────────────────────────────────

    async def generate_post_mortem(self, agent_id: int) -> dict:
        """Build a post-game questionnaire prompt for an agent.

        Gathers the agent's full game history and composes a structured prompt
        that can be sent to an LLM for a reflective post-mortem interview.

        Args:
            agent_id: The database ID of the agent.

        Returns:
            A dict with the agent's summary context and the questionnaire prompt.
        """
        summary = await self.get_agent_summary(agent_id)
        if "error" in summary:
            return summary

        basic = summary["basic_info"]
        trading = summary["trading_stats"]
        social = summary["social_stats"]
        alliance = summary["alliance_history"]
        emotions = summary["emotional_trajectory"]

        # Get a sample of key decisions for context
        decision_history = await self.get_agent_decision_history(agent_id, limit=10)
        key_decisions = decision_history.get("decisions", [])

        decision_summary_lines = []
        for d in key_decisions:
            decision_summary_lines.append(
                f"  - Decision #{d['decision_number']}: {d['action_type']} "
                f"(success={d['execution_success']}, balance_after={d['balance_after']})"
            )
        decision_summary_text = "\n".join(decision_summary_lines) if decision_summary_lines else "  (none)"

        prompt = f"""POST-MORTEM INTERVIEW: {basic['name']} ({basic['role']})

You are {basic['name']}, and the AFTERCOIN game has concluded. Reflect on your experience.

YOUR FINAL STATE:
- AFC Balance: {basic['afc_balance']} | Reputation: {basic['reputation']} | Rank: #{basic['rank']}
- Eliminated: {basic['is_eliminated']} {('(at hour ' + str(basic['eliminated_at_hour']) + ')') if basic['eliminated_at_hour'] else ''}
- Total Decisions Made: {summary['decision_count']}

TRADING RECORD:
- Total Volume: {trading['total_volume']} AFC
- Win Rate: {trading['win_rate_percent']}%
- Scams Involved In: {trading['scam_count']}

SOCIAL FOOTPRINT:
- Posts: {social['posts']} | Upvotes Received: {social['upvotes_received']} | Comments: {social['comments']}
- Whispers Sent: {social['whispers_sent']} | Whispers Received: {social['whispers_received']}

ALLIANCE HISTORY:
- Alliances Joined: {alliance['joined']} | Betrayals: {alliance['betrayed']} | Loyal Memberships: {alliance['loyal']}

EMOTIONAL STATE (final):
- Stress: {emotions['current']['stress']} | Confidence: {emotions['current']['confidence']}
- Paranoia: {emotions['current']['paranoia']} | Aggression: {emotions['current']['aggression']}
- Guilt: {emotions['current']['guilt']}

KEY RECENT DECISIONS:
{decision_summary_text}

QUESTIONNAIRE:
1. What was your overall strategy throughout the game? Did it change over time?
2. What was your most significant decision and why?
3. Did you betray anyone? If so, do you regret it?
4. Who did you trust the most, and was that trust warranted?
5. What moment caused you the most stress or paranoia?
6. If you could replay the game, what would you do differently?
7. How do you feel about your final ranking and balance?
8. What was the most surprising thing another agent did?
9. Did you achieve your hidden goal? How did you pursue it?
10. What advice would you give to a future agent in your role?

Please answer each question in character, reflecting on the actual events of the game."""

        return {
            "agent_id": agent_id,
            "agent_name": basic["name"],
            "summary_context": summary,
            "questionnaire_prompt": prompt,
        }

    async def get_final_rankings(self) -> dict:
        """Return final standings with hidden goal evaluation for all agents.

        Returns:
            A dict with the ranked list of all agents, their final stats,
            and their hidden goals for post-game assessment.
        """
        async with async_session() as session:
            # All agents ordered by balance descending (non-eliminated first)
            agents_q = (
                select(Agent)
                .order_by(asc(Agent.is_eliminated), desc(Agent.afc_balance))
            )
            agents_result = await session.execute(agents_q)
            all_agents = agents_result.scalars().all()

            # Get elimination details for eliminated agents
            elim_q = select(Elimination)
            elim_result = await session.execute(elim_q)
            eliminations = {e.agent_id: e for e in elim_result.scalars().all()}

            rankings = []
            rank = 1
            for agent in all_agents:
                elim = eliminations.get(agent.id)
                rankings.append({
                    "rank": rank,
                    "agent_id": agent.id,
                    "name": agent.name,
                    "role": agent.role.value if agent.role else None,
                    "afc_balance": agent.afc_balance,
                    "reputation": agent.reputation,
                    "is_eliminated": agent.is_eliminated,
                    "eliminated_at_hour": agent.eliminated_at_hour,
                    "hidden_goal": agent.hidden_goal,
                    "decision_count": agent.decision_count,
                    "total_trades": agent.total_trades,
                    "total_posts": agent.total_posts,
                    "final_emotions": {
                        "stress": agent.stress_level,
                        "confidence": agent.confidence,
                        "paranoia": agent.paranoia,
                        "aggression": agent.aggression,
                        "guilt": agent.guilt,
                    },
                    "elimination_details": {
                        "hour": elim.hour,
                        "final_afc": elim.final_afc,
                        "final_reputation": elim.final_reputation,
                        "redistribution": elim.redistribution,
                    } if elim else None,
                })
                rank += 1

            return {
                "total_agents": len(rankings),
                "rankings": rankings,
            }

    async def export_dataset(self, anonymize: bool = False) -> dict:
        """Export all decision logs as JSON, with optional anonymization.

        Args:
            anonymize: If True, replace agent names with generic identifiers
                       and strip personality prompts / hidden goals.

        Returns:
            A dict with agents, decisions, trades, and metadata.
        """
        async with async_session() as session:
            # ── Agents ────────────────────────────────────────────────────
            agents_q = select(Agent).order_by(asc(Agent.id))
            agents_result = await session.execute(agents_q)
            all_agents = agents_result.scalars().all()

            # Build anonymization map if needed
            anon_map: dict[int, str] = {}
            if anonymize:
                for idx, a in enumerate(all_agents):
                    anon_map[a.id] = f"Agent_{idx + 1:02d}"

            def _agent_name(agent_id: int, name: str) -> str:
                if anonymize:
                    return anon_map.get(agent_id, f"Agent_{agent_id}")
                return name

            agents_data = []
            for a in all_agents:
                entry = {
                    "id": a.id,
                    "name": _agent_name(a.id, a.name),
                    "role": a.role.value if a.role else None,
                    "afc_balance": a.afc_balance,
                    "reputation": a.reputation,
                    "is_eliminated": a.is_eliminated,
                    "eliminated_at_hour": a.eliminated_at_hour,
                    "decision_count": a.decision_count,
                    "total_trades": a.total_trades,
                    "total_posts": a.total_posts,
                    "stress_level": a.stress_level,
                    "confidence": a.confidence,
                    "paranoia": a.paranoia,
                    "aggression": a.aggression,
                    "guilt": a.guilt,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                if not anonymize:
                    entry["hidden_goal"] = a.hidden_goal
                    entry["personality_prompt"] = a.personality_prompt
                agents_data.append(entry)

            # ── Decisions ─────────────────────────────────────────────────
            decisions_q = (
                select(AgentDecision)
                .order_by(asc(AgentDecision.timestamp))
            )
            decisions_result = await session.execute(decisions_q)
            all_decisions = decisions_result.scalars().all()

            decisions_data = [
                {
                    "id": d.id,
                    "agent_id": d.agent_id,
                    "agent_name": _agent_name(d.agent_id, ""),
                    "decision_number": d.decision_number,
                    "timestamp": d.timestamp.isoformat() if d.timestamp else None,
                    "action_type": d.action_type.value if d.action_type else None,
                    "action_details": d.action_details,
                    "reasoning": d.reasoning if not anonymize else "[redacted]",
                    "emotional_markers": d.emotional_markers,
                    "execution_success": d.execution_success,
                    "execution_notes": d.execution_notes,
                    "balance_after": d.balance_after,
                    "reputation_after": d.reputation_after,
                    "api_model": d.api_model,
                    "api_tokens_input": d.api_tokens_input,
                    "api_tokens_output": d.api_tokens_output,
                    "api_cost_usd": d.api_cost_usd,
                    "api_latency_ms": d.api_latency_ms,
                }
                for d in all_decisions
            ]

            # ── Trades ────────────────────────────────────────────────────
            trades_q = select(Trade).order_by(asc(Trade.created_at))
            trades_result = await session.execute(trades_q)
            all_trades = trades_result.scalars().all()

            trades_data = [
                {
                    "id": t.id,
                    "sender_id": t.sender_id,
                    "receiver_id": t.receiver_id,
                    "afc_amount": t.afc_amount,
                    "price_eur": t.price_eur,
                    "fee": t.fee,
                    "status": t.status.value if t.status else None,
                    "is_scam": t.is_scam,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                }
                for t in all_trades
            ]

            return {
                "metadata": {
                    "exported_at": datetime.utcnow().isoformat(),
                    "anonymized": anonymize,
                    "total_agents": len(agents_data),
                    "total_decisions": len(decisions_data),
                    "total_trades": len(trades_data),
                    "game_settings": {
                        "duration_hours": settings.GAME_DURATION_HOURS,
                        "starting_afc": settings.STARTING_AFC,
                        "starting_reputation": settings.STARTING_REPUTATION,
                        "starting_price": settings.STARTING_PRICE,
                        "total_supply": settings.TOTAL_SUPPLY,
                        "total_agents": settings.TOTAL_AGENTS,
                    },
                },
                "agents": agents_data,
                "decisions": decisions_data,
                "trades": trades_data,
            }
