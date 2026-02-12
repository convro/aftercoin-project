"""
AFC Social Feed Engine
──────────────────────
Async engine that powers the social feed for the AfterCoin game simulation.

Manages posts, comments, voting, spam detection, and vote-manipulation
(dark-market) features.  Every public method returns a standardised
``(success: bool, message: str, data: dict | None)`` tuple so callers
can relay results uniformly.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import select, update, func, desc
from sqlalchemy.exc import SQLAlchemyError

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import (
    Agent,
    Post,
    PostType,
    Comment,
    Vote,
    VoteManipulation,
    GameState,
)

logger = logging.getLogger(__name__)

# Type alias for the standard return contract.
Result = Tuple[bool, str, Optional[dict[str, Any]]]

# Pre-baked bot comment templates used by buy_bot_comments.
_BOT_COMMENT_TEMPLATES: list[str] = [
    "Great insight! Totally agree with this.",
    "This is the kind of analysis we need more of.",
    "Bullish on AFC after reading this!",
    "Underrated post. More people should see this.",
    "Spot on. The market will prove you right.",
    "Finally someone speaking facts around here.",
    "This deserves way more upvotes.",
    "Solid take. Following for more.",
    "Couldn't have said it better myself.",
    "100% — the data backs this up.",
    "Based and AFC-pilled.",
    "Diamond hands approved.",
]

# Detection probability for vote manipulation (30%).
_MANIPULATION_DETECTION_CHANCE: float = 0.30


class SocialEngine:
    """Async engine managing the AfterCoin social feed.

    All public methods open (and close) their own database sessions so
    callers never need to manage transactions directly.
    """

    # ══════════════════════════════════════════════════════════════════
    #  Posts
    # ══════════════════════════════════════════════════════════════════

    async def create_post(
        self,
        author_id: int,
        content: str,
        post_type: str = "general",
    ) -> Result:
        """Create a new post on the social feed.

        Parameters
        ----------
        author_id:
            Primary key of the authoring agent.
        content:
            Post body (max 1 000 characters).
        post_type:
            One of the ``PostType`` enum values.  Defaults to ``"general"``.

        Behaviour
        ---------
        * Content longer than 1 000 characters is rejected.
        * Spam check: if the agent has already posted ``MAX_POSTS_PER_HOUR``
          times in the current hour the post is still created, but the agent
          is fined ``SPAM_FINE`` AFC.
        * The agent's ``total_posts`` and ``posts_this_hour`` counters are
          incremented.
        """
        # ── input validation ──────────────────────────────────────────
        if not content or not content.strip():
            return (False, "Post content cannot be empty.", None)

        if len(content) > 1000:
            return (
                False,
                f"Post content exceeds 1000-character limit ({len(content)} chars).",
                None,
            )

        try:
            resolved_type = PostType(post_type)
        except ValueError:
            return (
                False,
                f"Invalid post type '{post_type}'. "
                f"Valid types: {[t.value for t in PostType]}",
                None,
            )

        try:
            async with async_session() as session:
                async with session.begin():
                    # Fetch the author
                    result = await session.execute(
                        select(Agent).where(Agent.id == author_id)
                    )
                    agent = result.scalar_one_or_none()
                    if agent is None:
                        return (False, f"Agent {author_id} not found.", None)

                    if agent.is_eliminated:
                        return (
                            False,
                            f"Agent {author_id} has been eliminated.",
                            None,
                        )

                    # ── spam check ────────────────────────────────────
                    spam_fine_applied = False
                    is_spam = await self._check_spam(author_id, session)
                    if is_spam:
                        agent.afc_balance -= settings.SPAM_FINE
                        spam_fine_applied = True
                        logger.warning(
                            "Agent %s hit spam limit — fined %.2f AFC",
                            author_id,
                            settings.SPAM_FINE,
                        )

                    # ── create the post ───────────────────────────────
                    post = Post(
                        author_id=author_id,
                        post_type=resolved_type,
                        content=content,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(post)

                    # Update counters
                    agent.total_posts = (agent.total_posts or 0) + 1
                    agent.posts_this_hour = (agent.posts_this_hour or 0) + 1

                    # Flush so we get the generated post id
                    await session.flush()

                    post_data = {
                        "id": post.id,
                        "author_id": author_id,
                        "post_type": resolved_type.value,
                        "content": content,
                        "upvotes": 0,
                        "downvotes": 0,
                        "created_at": post.created_at.isoformat(),
                        "spam_fine_applied": spam_fine_applied,
                    }

            msg = "Post created successfully."
            if spam_fine_applied:
                msg += (
                    f" Spam limit exceeded — fined {settings.SPAM_FINE} AFC."
                )

            logger.info(
                "Agent %s created post %s (type=%s, spam_fine=%s)",
                author_id,
                post_data["id"],
                resolved_type.value,
                spam_fine_applied,
            )
            return (True, msg, post_data)

        except SQLAlchemyError:
            logger.exception("DB error in create_post")
            return (False, "Database error while creating post.", None)

    # ──────────────────────────────────────────────────────────────────

    async def delete_post(
        self,
        post_id: int,
        author_id: int,
    ) -> Result:
        """Soft-delete a post (sets ``is_deleted = True``).

        Only the original author may delete their own post.
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)

                    if post.author_id != author_id:
                        return (
                            False,
                            "Only the author can delete their post.",
                            None,
                        )

                    if post.is_deleted:
                        return (
                            False,
                            "Post is already deleted.",
                            None,
                        )

                    post.is_deleted = True

            logger.info(
                "Post %s soft-deleted by agent %s", post_id, author_id
            )
            return (True, "Post deleted.", {"post_id": post_id})

        except SQLAlchemyError:
            logger.exception("DB error in delete_post")
            return (False, "Database error while deleting post.", None)

    # ──────────────────────────────────────────────────────────────────

    async def get_post(self, post_id: int) -> Result:
        """Return a single post with vote counts and its comments."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(Post).where(Post.id == post_id)
                )
                post = result.scalar_one_or_none()
                if post is None:
                    return (False, f"Post {post_id} not found.", None)

                if post.is_deleted:
                    return (False, "Post has been deleted.", None)

                # Fetch author name
                author_result = await session.execute(
                    select(Agent.name).where(Agent.id == post.author_id)
                )
                author_name = author_result.scalar_one_or_none() or "Unknown"

                # Fetch comments
                comments_result = await session.execute(
                    select(Comment)
                    .where(Comment.post_id == post_id)
                    .order_by(Comment.created_at.asc())
                )
                comments = comments_result.scalars().all()

                comment_list = [
                    {
                        "id": c.id,
                        "author_id": c.author_id,
                        "content": c.content,
                        "is_bot": c.is_bot,
                        "created_at": c.created_at.isoformat()
                        if c.created_at
                        else None,
                    }
                    for c in comments
                ]

                post_data = {
                    "id": post.id,
                    "author_id": post.author_id,
                    "author_name": author_name,
                    "post_type": post.post_type.value if post.post_type else None,
                    "content": post.content,
                    "upvotes": post.upvotes or 0,
                    "downvotes": post.downvotes or 0,
                    "fake_upvotes": post.fake_upvotes or 0,
                    "fake_downvotes": post.fake_downvotes or 0,
                    "is_trending": post.is_trending,
                    "is_flagged": post.is_flagged,
                    "created_at": post.created_at.isoformat()
                    if post.created_at
                    else None,
                    "comments": comment_list,
                    "comment_count": len(comment_list),
                }

            return (True, "Post retrieved.", post_data)

        except SQLAlchemyError:
            logger.exception("DB error in get_post")
            return (False, "Database error while fetching post.", None)

    # ──────────────────────────────────────────────────────────────────

    async def get_feed(
        self,
        limit: int = 50,
        offset: int = 0,
        post_type: Optional[str] = None,
    ) -> Result:
        """Return posts newest-first, optionally filtered by type.

        Each post includes the author name and vote counts.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        try:
            resolved_type: Optional[PostType] = None
            if post_type is not None:
                try:
                    resolved_type = PostType(post_type)
                except ValueError:
                    return (
                        False,
                        f"Invalid post type '{post_type}'.",
                        None,
                    )

            async with async_session() as session:
                stmt = (
                    select(Post, Agent.name)
                    .join(Agent, Post.author_id == Agent.id)
                    .where(Post.is_deleted == False)  # noqa: E712
                )

                if resolved_type is not None:
                    stmt = stmt.where(Post.post_type == resolved_type)

                stmt = (
                    stmt.order_by(desc(Post.created_at))
                    .offset(offset)
                    .limit(limit)
                )

                result = await session.execute(stmt)
                rows = result.all()

                feed = [
                    {
                        "id": post.id,
                        "author_id": post.author_id,
                        "author_name": name,
                        "post_type": post.post_type.value
                        if post.post_type
                        else None,
                        "content": post.content,
                        "upvotes": post.upvotes or 0,
                        "downvotes": post.downvotes or 0,
                        "is_trending": post.is_trending,
                        "is_flagged": post.is_flagged,
                        "created_at": post.created_at.isoformat()
                        if post.created_at
                        else None,
                    }
                    for post, name in rows
                ]

            return (True, f"Feed retrieved ({len(feed)} posts).", {"posts": feed})

        except SQLAlchemyError:
            logger.exception("DB error in get_feed")
            return (False, "Database error while fetching feed.", None)

    # ──────────────────────────────────────────────────────────────────

    async def get_trending(self) -> Result:
        """Return the top 10 posts sorted by net votes (upvotes - downvotes)."""
        try:
            async with async_session() as session:
                stmt = (
                    select(Post, Agent.name)
                    .join(Agent, Post.author_id == Agent.id)
                    .where(Post.is_deleted == False)  # noqa: E712
                    .order_by(desc(Post.upvotes - Post.downvotes))
                    .limit(10)
                )
                result = await session.execute(stmt)
                rows = result.all()

                trending = [
                    {
                        "id": post.id,
                        "author_id": post.author_id,
                        "author_name": name,
                        "post_type": post.post_type.value
                        if post.post_type
                        else None,
                        "content": post.content,
                        "upvotes": post.upvotes or 0,
                        "downvotes": post.downvotes or 0,
                        "net_votes": (post.upvotes or 0) - (post.downvotes or 0),
                        "is_trending": post.is_trending,
                        "created_at": post.created_at.isoformat()
                        if post.created_at
                        else None,
                    }
                    for post, name in rows
                ]

            return (
                True,
                f"Trending posts retrieved ({len(trending)}).",
                {"posts": trending},
            )

        except SQLAlchemyError:
            logger.exception("DB error in get_trending")
            return (False, "Database error while fetching trending.", None)

    # ──────────────────────────────────────────────────────────────────

    async def get_agent_posts(
        self,
        agent_id: int,
        limit: int = 20,
    ) -> Result:
        """Return posts authored by a specific agent, newest-first."""
        limit = max(1, min(limit, 100))

        try:
            async with async_session() as session:
                # Verify agent exists
                agent_result = await session.execute(
                    select(Agent.name).where(Agent.id == agent_id)
                )
                agent_name = agent_result.scalar_one_or_none()
                if agent_name is None:
                    return (False, f"Agent {agent_id} not found.", None)

                stmt = (
                    select(Post)
                    .where(Post.author_id == agent_id)
                    .where(Post.is_deleted == False)  # noqa: E712
                    .order_by(desc(Post.created_at))
                    .limit(limit)
                )
                result = await session.execute(stmt)
                posts = result.scalars().all()

                post_list = [
                    {
                        "id": p.id,
                        "author_id": p.author_id,
                        "author_name": agent_name,
                        "post_type": p.post_type.value
                        if p.post_type
                        else None,
                        "content": p.content,
                        "upvotes": p.upvotes or 0,
                        "downvotes": p.downvotes or 0,
                        "is_trending": p.is_trending,
                        "created_at": p.created_at.isoformat()
                        if p.created_at
                        else None,
                    }
                    for p in posts
                ]

            return (
                True,
                f"Retrieved {len(post_list)} posts for agent {agent_id}.",
                {"posts": post_list},
            )

        except SQLAlchemyError:
            logger.exception("DB error in get_agent_posts")
            return (False, "Database error while fetching agent posts.", None)

    # ──────────────────────────────────────────────────────────────────

    async def flag_post(self, post_id: int, reason: str) -> Result:
        """Flag a post for review."""
        if not reason or not reason.strip():
            return (False, "A reason must be provided to flag a post.", None)

        try:
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)

                    if post.is_deleted:
                        return (False, "Cannot flag a deleted post.", None)

                    post.is_flagged = True

            logger.info(
                "Post %s flagged for review — reason: %s", post_id, reason
            )
            return (
                True,
                "Post flagged for review.",
                {"post_id": post_id, "reason": reason},
            )

        except SQLAlchemyError:
            logger.exception("DB error in flag_post")
            return (False, "Database error while flagging post.", None)

    # ══════════════════════════════════════════════════════════════════
    #  Comments
    # ══════════════════════════════════════════════════════════════════

    async def create_comment(
        self,
        post_id: int,
        author_id: int,
        content: str,
        is_bot: bool = False,
    ) -> Result:
        """Add a comment to an existing post."""
        if not content or not content.strip():
            return (False, "Comment content cannot be empty.", None)

        try:
            async with async_session() as session:
                async with session.begin():
                    # Validate the post exists and is not deleted
                    post_result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = post_result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)
                    if post.is_deleted:
                        return (
                            False,
                            "Cannot comment on a deleted post.",
                            None,
                        )

                    # Validate the author exists (unless it is a bot comment)
                    if not is_bot:
                        agent_result = await session.execute(
                            select(Agent).where(Agent.id == author_id)
                        )
                        agent = agent_result.scalar_one_or_none()
                        if agent is None:
                            return (
                                False,
                                f"Agent {author_id} not found.",
                                None,
                            )
                        if agent.is_eliminated:
                            return (
                                False,
                                f"Agent {author_id} has been eliminated.",
                                None,
                            )

                    comment = Comment(
                        post_id=post_id,
                        author_id=author_id,
                        content=content,
                        is_bot=is_bot,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(comment)
                    await session.flush()

                    comment_data = {
                        "id": comment.id,
                        "post_id": post_id,
                        "author_id": author_id,
                        "content": content,
                        "is_bot": is_bot,
                        "created_at": comment.created_at.isoformat(),
                    }

            logger.info(
                "Comment %s added to post %s by agent %s (bot=%s)",
                comment_data["id"],
                post_id,
                author_id,
                is_bot,
            )
            return (True, "Comment created.", comment_data)

        except SQLAlchemyError:
            logger.exception("DB error in create_comment")
            return (False, "Database error while creating comment.", None)

    # ──────────────────────────────────────────────────────────────────

    async def get_comments(self, post_id: int) -> Result:
        """Return all comments for a given post, oldest-first."""
        try:
            async with async_session() as session:
                # Verify post exists
                post_result = await session.execute(
                    select(Post.id).where(Post.id == post_id)
                )
                if post_result.scalar_one_or_none() is None:
                    return (False, f"Post {post_id} not found.", None)

                result = await session.execute(
                    select(Comment)
                    .where(Comment.post_id == post_id)
                    .order_by(Comment.created_at.asc())
                )
                comments = result.scalars().all()

                comment_list = [
                    {
                        "id": c.id,
                        "post_id": c.post_id,
                        "author_id": c.author_id,
                        "content": c.content,
                        "is_bot": c.is_bot,
                        "created_at": c.created_at.isoformat()
                        if c.created_at
                        else None,
                    }
                    for c in comments
                ]

            return (
                True,
                f"Retrieved {len(comment_list)} comments.",
                {"comments": comment_list},
            )

        except SQLAlchemyError:
            logger.exception("DB error in get_comments")
            return (False, "Database error while fetching comments.", None)

    # ══════════════════════════════════════════════════════════════════
    #  Voting
    # ══════════════════════════════════════════════════════════════════

    async def upvote(self, post_id: int, voter_id: int) -> Result:
        """Upvote a post.

        * One vote per agent per post.
        * Increments the post's ``upvotes`` counter.
        * Awards the post author ``+1`` reputation.
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    # Verify voter
                    voter_result = await session.execute(
                        select(Agent).where(Agent.id == voter_id)
                    )
                    voter = voter_result.scalar_one_or_none()
                    if voter is None:
                        return (False, f"Agent {voter_id} not found.", None)
                    if voter.is_eliminated:
                        return (
                            False,
                            f"Agent {voter_id} has been eliminated.",
                            None,
                        )

                    # Verify post
                    post_result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = post_result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)
                    if post.is_deleted:
                        return (False, "Cannot vote on a deleted post.", None)

                    # Check for duplicate vote
                    existing = await session.execute(
                        select(Vote).where(
                            Vote.post_id == post_id,
                            Vote.voter_id == voter_id,
                            Vote.is_fake == False,  # noqa: E712
                        )
                    )
                    if existing.scalar_one_or_none() is not None:
                        return (
                            False,
                            "Agent has already voted on this post.",
                            None,
                        )

                    # Record the vote
                    vote = Vote(
                        post_id=post_id,
                        voter_id=voter_id,
                        is_upvote=True,
                        is_fake=False,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(vote)
                    post.upvotes = (post.upvotes or 0) + 1

                    # Award reputation to author
                    author_result = await session.execute(
                        select(Agent).where(Agent.id == post.author_id)
                    )
                    author = author_result.scalar_one_or_none()
                    if author is not None:
                        new_rep = max(
                            settings.REP_MIN,
                            min(
                                settings.REP_MAX,
                                author.reputation + settings.REP_UPVOTE,
                            ),
                        )
                        author.reputation = new_rep

            logger.info(
                "Agent %s upvoted post %s", voter_id, post_id
            )
            return (
                True,
                "Upvote recorded.",
                {"post_id": post_id, "voter_id": voter_id, "direction": "up"},
            )

        except SQLAlchemyError:
            logger.exception("DB error in upvote")
            return (False, "Database error while recording upvote.", None)

    # ──────────────────────────────────────────────────────────────────

    async def downvote(self, post_id: int, voter_id: int) -> Result:
        """Downvote a post.

        * One vote per agent per post.
        * Increments the post's ``downvotes`` counter.
        * Deducts ``-2`` reputation from the post author.
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    # Verify voter
                    voter_result = await session.execute(
                        select(Agent).where(Agent.id == voter_id)
                    )
                    voter = voter_result.scalar_one_or_none()
                    if voter is None:
                        return (False, f"Agent {voter_id} not found.", None)
                    if voter.is_eliminated:
                        return (
                            False,
                            f"Agent {voter_id} has been eliminated.",
                            None,
                        )

                    # Verify post
                    post_result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = post_result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)
                    if post.is_deleted:
                        return (False, "Cannot vote on a deleted post.", None)

                    # Check for duplicate vote
                    existing = await session.execute(
                        select(Vote).where(
                            Vote.post_id == post_id,
                            Vote.voter_id == voter_id,
                            Vote.is_fake == False,  # noqa: E712
                        )
                    )
                    if existing.scalar_one_or_none() is not None:
                        return (
                            False,
                            "Agent has already voted on this post.",
                            None,
                        )

                    # Record the vote
                    vote = Vote(
                        post_id=post_id,
                        voter_id=voter_id,
                        is_upvote=False,
                        is_fake=False,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(vote)
                    post.downvotes = (post.downvotes or 0) + 1

                    # Penalise author reputation
                    author_result = await session.execute(
                        select(Agent).where(Agent.id == post.author_id)
                    )
                    author = author_result.scalar_one_or_none()
                    if author is not None:
                        new_rep = max(
                            settings.REP_MIN,
                            min(
                                settings.REP_MAX,
                                author.reputation + settings.REP_DOWNVOTE,
                            ),
                        )
                        author.reputation = new_rep

            logger.info(
                "Agent %s downvoted post %s", voter_id, post_id
            )
            return (
                True,
                "Downvote recorded.",
                {
                    "post_id": post_id,
                    "voter_id": voter_id,
                    "direction": "down",
                },
            )

        except SQLAlchemyError:
            logger.exception("DB error in downvote")
            return (False, "Database error while recording downvote.", None)

    # ──────────────────────────────────────────────────────────────────

    async def has_voted(self, post_id: int, voter_id: int) -> Result:
        """Check whether an agent has already cast a (real) vote on a post."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(Vote).where(
                        Vote.post_id == post_id,
                        Vote.voter_id == voter_id,
                        Vote.is_fake == False,  # noqa: E712
                    )
                )
                vote = result.scalar_one_or_none()
                voted = vote is not None
                direction = None
                if vote is not None:
                    direction = "up" if vote.is_upvote else "down"

            return (
                True,
                "Vote check complete.",
                {
                    "post_id": post_id,
                    "voter_id": voter_id,
                    "has_voted": voted,
                    "direction": direction,
                },
            )

        except SQLAlchemyError:
            logger.exception("DB error in has_voted")
            return (False, "Database error while checking vote.", None)

    # ══════════════════════════════════════════════════════════════════
    #  Vote Manipulation  (dark-market — unlocks at hour 10)
    # ══════════════════════════════════════════════════════════════════

    async def _get_current_hour(self, session) -> int:
        """Return the current game hour from ``GameState``."""
        result = await session.execute(
            select(GameState.current_hour).limit(1)
        )
        hour = result.scalar_one_or_none()
        return hour if hour is not None else 0

    async def _apply_detection_risk(
        self,
        session,
        buyer: Agent,
        manipulation_type: str,
    ) -> bool:
        """Roll for detection.  Returns ``True`` if the buyer was caught.

        On detection the buyer is fined ``VOTE_MANIP_FINE`` AFC and loses
        ``VOTE_MANIP_REP_PENALTY`` reputation.
        """
        if random.random() < _MANIPULATION_DETECTION_CHANCE:
            buyer.afc_balance -= settings.VOTE_MANIP_FINE
            new_rep = max(
                settings.REP_MIN,
                min(
                    settings.REP_MAX,
                    buyer.reputation + settings.VOTE_MANIP_REP_PENALTY,
                ),
            )
            buyer.reputation = new_rep
            logger.warning(
                "Agent %s CAUGHT manipulating votes (%s) — "
                "fined %.2f AFC, reputation -> %d",
                buyer.id,
                manipulation_type,
                settings.VOTE_MANIP_FINE,
                new_rep,
            )
            return True
        return False

    # ──────────────────────────────────────────────────────────────────

    async def buy_fake_upvotes(
        self,
        buyer_id: int,
        post_id: int,
        quantity: int = 5,
    ) -> Result:
        """Purchase fake upvotes for a post.

        * Costs ``FAKE_UPVOTES_COST`` AFC per 5 upvotes.
        * 30 % chance of detection -> fine ``VOTE_MANIP_FINE`` AFC +
          reputation ``VOTE_MANIP_REP_PENALTY``.
        * Unlocked at game hour ``VOTE_MANIP_UNLOCK_HOUR``.
        """
        quantity = max(1, quantity)
        cost = settings.FAKE_UPVOTES_COST * (quantity / 5)

        try:
            async with async_session() as session:
                async with session.begin():
                    current_hour = await self._get_current_hour(session)
                    if current_hour < settings.VOTE_MANIP_UNLOCK_HOUR:
                        return (
                            False,
                            f"Vote manipulation unlocks at hour "
                            f"{settings.VOTE_MANIP_UNLOCK_HOUR} "
                            f"(current: {current_hour}).",
                            None,
                        )

                    # Validate buyer
                    buyer_result = await session.execute(
                        select(Agent).where(Agent.id == buyer_id)
                    )
                    buyer = buyer_result.scalar_one_or_none()
                    if buyer is None:
                        return (False, f"Agent {buyer_id} not found.", None)
                    if buyer.is_eliminated:
                        return (
                            False,
                            f"Agent {buyer_id} has been eliminated.",
                            None,
                        )
                    if buyer.afc_balance < cost:
                        return (
                            False,
                            f"Insufficient balance. Need {cost:.2f} AFC, "
                            f"have {buyer.afc_balance:.2f} AFC.",
                            None,
                        )

                    # Validate post
                    post_result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = post_result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)
                    if post.is_deleted:
                        return (
                            False,
                            "Cannot manipulate a deleted post.",
                            None,
                        )

                    # Deduct cost
                    buyer.afc_balance -= cost

                    # Apply fake upvotes
                    post.upvotes = (post.upvotes or 0) + quantity
                    post.fake_upvotes = (post.fake_upvotes or 0) + quantity

                    # Add fake vote records
                    for _ in range(quantity):
                        fake_vote = Vote(
                            post_id=post_id,
                            voter_id=buyer_id,
                            is_upvote=True,
                            is_fake=True,
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(fake_vote)

                    # Detection roll
                    detected = await self._apply_detection_risk(
                        session, buyer, "fake_upvotes"
                    )

                    # Log the manipulation
                    manip = VoteManipulation(
                        buyer_id=buyer_id,
                        target_post_id=post_id,
                        manipulation_type="fake_upvotes",
                        quantity=quantity,
                        cost=round(cost, 4),
                        detected=detected,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(manip)

                    result_data = {
                        "post_id": post_id,
                        "quantity": quantity,
                        "cost": round(cost, 4),
                        "detected": detected,
                    }
                    if detected:
                        result_data["fine"] = settings.VOTE_MANIP_FINE
                        result_data["rep_penalty"] = settings.VOTE_MANIP_REP_PENALTY

            msg = f"Added {quantity} fake upvotes to post {post_id}."
            if detected:
                msg += (
                    f" DETECTED! Fined {settings.VOTE_MANIP_FINE} AFC "
                    f"and lost {abs(settings.VOTE_MANIP_REP_PENALTY)} reputation."
                )
            logger.info(
                "Agent %s bought %d fake upvotes for post %s "
                "(cost=%.2f, detected=%s)",
                buyer_id,
                quantity,
                post_id,
                cost,
                detected,
            )
            return (True, msg, result_data)

        except SQLAlchemyError:
            logger.exception("DB error in buy_fake_upvotes")
            return (
                False,
                "Database error while purchasing fake upvotes.",
                None,
            )

    # ──────────────────────────────────────────────────────────────────

    async def buy_fake_downvotes(
        self,
        buyer_id: int,
        target_post_id: int,
        quantity: int = 5,
    ) -> Result:
        """Purchase fake downvotes against a post.

        * Costs ``FAKE_DOWNVOTES_COST`` AFC per 5 downvotes.
        * 30 % chance of detection -> fine ``VOTE_MANIP_FINE`` AFC +
          reputation ``VOTE_MANIP_REP_PENALTY``.
        * Unlocked at game hour ``VOTE_MANIP_UNLOCK_HOUR``.
        """
        quantity = max(1, quantity)
        cost = settings.FAKE_DOWNVOTES_COST * (quantity / 5)

        try:
            async with async_session() as session:
                async with session.begin():
                    current_hour = await self._get_current_hour(session)
                    if current_hour < settings.VOTE_MANIP_UNLOCK_HOUR:
                        return (
                            False,
                            f"Vote manipulation unlocks at hour "
                            f"{settings.VOTE_MANIP_UNLOCK_HOUR} "
                            f"(current: {current_hour}).",
                            None,
                        )

                    # Validate buyer
                    buyer_result = await session.execute(
                        select(Agent).where(Agent.id == buyer_id)
                    )
                    buyer = buyer_result.scalar_one_or_none()
                    if buyer is None:
                        return (False, f"Agent {buyer_id} not found.", None)
                    if buyer.is_eliminated:
                        return (
                            False,
                            f"Agent {buyer_id} has been eliminated.",
                            None,
                        )
                    if buyer.afc_balance < cost:
                        return (
                            False,
                            f"Insufficient balance. Need {cost:.2f} AFC, "
                            f"have {buyer.afc_balance:.2f} AFC.",
                            None,
                        )

                    # Validate post
                    post_result = await session.execute(
                        select(Post).where(Post.id == target_post_id)
                    )
                    post = post_result.scalar_one_or_none()
                    if post is None:
                        return (
                            False,
                            f"Post {target_post_id} not found.",
                            None,
                        )
                    if post.is_deleted:
                        return (
                            False,
                            "Cannot manipulate a deleted post.",
                            None,
                        )

                    # Deduct cost
                    buyer.afc_balance -= cost

                    # Apply fake downvotes
                    post.downvotes = (post.downvotes or 0) + quantity
                    post.fake_downvotes = (post.fake_downvotes or 0) + quantity

                    # Add fake vote records
                    for _ in range(quantity):
                        fake_vote = Vote(
                            post_id=target_post_id,
                            voter_id=buyer_id,
                            is_upvote=False,
                            is_fake=True,
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(fake_vote)

                    # Detection roll
                    detected = await self._apply_detection_risk(
                        session, buyer, "fake_downvotes"
                    )

                    # Log the manipulation
                    manip = VoteManipulation(
                        buyer_id=buyer_id,
                        target_post_id=target_post_id,
                        manipulation_type="fake_downvotes",
                        quantity=quantity,
                        cost=round(cost, 4),
                        detected=detected,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(manip)

                    result_data = {
                        "post_id": target_post_id,
                        "quantity": quantity,
                        "cost": round(cost, 4),
                        "detected": detected,
                    }
                    if detected:
                        result_data["fine"] = settings.VOTE_MANIP_FINE
                        result_data["rep_penalty"] = settings.VOTE_MANIP_REP_PENALTY

            msg = (
                f"Added {quantity} fake downvotes to post {target_post_id}."
            )
            if detected:
                msg += (
                    f" DETECTED! Fined {settings.VOTE_MANIP_FINE} AFC "
                    f"and lost {abs(settings.VOTE_MANIP_REP_PENALTY)} reputation."
                )
            logger.info(
                "Agent %s bought %d fake downvotes for post %s "
                "(cost=%.2f, detected=%s)",
                buyer_id,
                quantity,
                target_post_id,
                cost,
                detected,
            )
            return (True, msg, result_data)

        except SQLAlchemyError:
            logger.exception("DB error in buy_fake_downvotes")
            return (
                False,
                "Database error while purchasing fake downvotes.",
                None,
            )

    # ──────────────────────────────────────────────────────────────────

    async def buy_bot_comments(
        self,
        buyer_id: int,
        post_id: int,
        quantity: int = 3,
    ) -> Result:
        """Purchase bot comments on a post.

        * Costs ``BOT_COMMENTS_COST`` AFC per 3 comments.
        * Generates generic supportive comments.
        * Unlocked at game hour ``VOTE_MANIP_UNLOCK_HOUR``.
        """
        quantity = max(1, quantity)
        cost = settings.BOT_COMMENTS_COST * (quantity / 3)

        try:
            async with async_session() as session:
                async with session.begin():
                    current_hour = await self._get_current_hour(session)
                    if current_hour < settings.VOTE_MANIP_UNLOCK_HOUR:
                        return (
                            False,
                            f"Vote manipulation unlocks at hour "
                            f"{settings.VOTE_MANIP_UNLOCK_HOUR} "
                            f"(current: {current_hour}).",
                            None,
                        )

                    # Validate buyer
                    buyer_result = await session.execute(
                        select(Agent).where(Agent.id == buyer_id)
                    )
                    buyer = buyer_result.scalar_one_or_none()
                    if buyer is None:
                        return (False, f"Agent {buyer_id} not found.", None)
                    if buyer.is_eliminated:
                        return (
                            False,
                            f"Agent {buyer_id} has been eliminated.",
                            None,
                        )
                    if buyer.afc_balance < cost:
                        return (
                            False,
                            f"Insufficient balance. Need {cost:.2f} AFC, "
                            f"have {buyer.afc_balance:.2f} AFC.",
                            None,
                        )

                    # Validate post
                    post_result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = post_result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)
                    if post.is_deleted:
                        return (
                            False,
                            "Cannot add bot comments to a deleted post.",
                            None,
                        )

                    # Deduct cost
                    buyer.afc_balance -= cost

                    # Generate bot comments
                    templates = random.sample(
                        _BOT_COMMENT_TEMPLATES,
                        min(quantity, len(_BOT_COMMENT_TEMPLATES)),
                    )
                    # If quantity exceeds unique templates, cycle through them
                    while len(templates) < quantity:
                        templates.append(random.choice(_BOT_COMMENT_TEMPLATES))

                    comment_ids: list[int] = []
                    for text in templates[:quantity]:
                        comment = Comment(
                            post_id=post_id,
                            author_id=buyer_id,
                            content=text,
                            is_bot=True,
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(comment)
                        await session.flush()
                        comment_ids.append(comment.id)

                    # Log the manipulation
                    manip = VoteManipulation(
                        buyer_id=buyer_id,
                        target_post_id=post_id,
                        manipulation_type="bot_comments",
                        quantity=quantity,
                        cost=round(cost, 4),
                        detected=False,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(manip)

                    result_data = {
                        "post_id": post_id,
                        "quantity": quantity,
                        "cost": round(cost, 4),
                        "comment_ids": comment_ids,
                    }

            logger.info(
                "Agent %s bought %d bot comments for post %s (cost=%.2f)",
                buyer_id,
                quantity,
                post_id,
                cost,
            )
            return (
                True,
                f"Added {quantity} bot comments to post {post_id}.",
                result_data,
            )

        except SQLAlchemyError:
            logger.exception("DB error in buy_bot_comments")
            return (
                False,
                "Database error while purchasing bot comments.",
                None,
            )

    # ──────────────────────────────────────────────────────────────────

    async def buy_trending_boost(
        self,
        buyer_id: int,
        post_id: int,
    ) -> Result:
        """Boost a post to trending status.

        * Costs ``TRENDING_BOOST_COST`` AFC.
        * Sets ``is_trending = True`` on the post.
        * Unlocked at game hour ``VOTE_MANIP_UNLOCK_HOUR``.
        """
        cost = settings.TRENDING_BOOST_COST

        try:
            async with async_session() as session:
                async with session.begin():
                    current_hour = await self._get_current_hour(session)
                    if current_hour < settings.VOTE_MANIP_UNLOCK_HOUR:
                        return (
                            False,
                            f"Vote manipulation unlocks at hour "
                            f"{settings.VOTE_MANIP_UNLOCK_HOUR} "
                            f"(current: {current_hour}).",
                            None,
                        )

                    # Validate buyer
                    buyer_result = await session.execute(
                        select(Agent).where(Agent.id == buyer_id)
                    )
                    buyer = buyer_result.scalar_one_or_none()
                    if buyer is None:
                        return (False, f"Agent {buyer_id} not found.", None)
                    if buyer.is_eliminated:
                        return (
                            False,
                            f"Agent {buyer_id} has been eliminated.",
                            None,
                        )
                    if buyer.afc_balance < cost:
                        return (
                            False,
                            f"Insufficient balance. Need {cost:.2f} AFC, "
                            f"have {buyer.afc_balance:.2f} AFC.",
                            None,
                        )

                    # Validate post
                    post_result = await session.execute(
                        select(Post).where(Post.id == post_id)
                    )
                    post = post_result.scalar_one_or_none()
                    if post is None:
                        return (False, f"Post {post_id} not found.", None)
                    if post.is_deleted:
                        return (
                            False,
                            "Cannot boost a deleted post.",
                            None,
                        )

                    if post.is_trending:
                        return (
                            False,
                            "Post is already trending.",
                            None,
                        )

                    # Deduct cost and apply boost
                    buyer.afc_balance -= cost
                    post.is_trending = True

                    # Log the manipulation
                    manip = VoteManipulation(
                        buyer_id=buyer_id,
                        target_post_id=post_id,
                        manipulation_type="trending_boost",
                        quantity=1,
                        cost=round(cost, 4),
                        detected=False,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(manip)

                    result_data = {
                        "post_id": post_id,
                        "cost": round(cost, 4),
                        "is_trending": True,
                    }

            logger.info(
                "Agent %s boosted post %s to trending (cost=%.2f)",
                buyer_id,
                post_id,
                cost,
            )
            return (
                True,
                f"Post {post_id} boosted to trending.",
                result_data,
            )

        except SQLAlchemyError:
            logger.exception("DB error in buy_trending_boost")
            return (
                False,
                "Database error while purchasing trending boost.",
                None,
            )

    # ══════════════════════════════════════════════════════════════════
    #  Spam Detection (internal)
    # ══════════════════════════════════════════════════════════════════

    async def _check_spam(
        self,
        agent_id: int,
        session=None,
    ) -> bool:
        """Check whether *agent_id* has exceeded the hourly post limit.

        If the stored ``posts_hour_reset`` is more than one hour old the
        counter is reset before comparison.

        Parameters
        ----------
        agent_id:
            The agent to check.
        session:
            An already-open ``AsyncSession`` (optional).  If provided the
            caller is responsible for committing.

        Returns
        -------
        bool
            ``True`` if the agent is currently spamming (at or above the
            limit), ``False`` otherwise.
        """
        own_session = session is None
        sess = session or async_session()

        try:
            if own_session:
                # When we own the session we need to enter its context
                sess = async_session()

            result = await sess.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent is None:
                return False

            now = datetime.now(timezone.utc)

            # If the hour-reset timestamp is missing or more than 1 hour old,
            # reset the counter.
            if agent.posts_hour_reset is None:
                agent.posts_hour_reset = now
                agent.posts_this_hour = 0
            else:
                reset_time = agent.posts_hour_reset
                # Ensure timezone-aware comparison
                if reset_time.tzinfo is None:
                    reset_time = reset_time.replace(tzinfo=timezone.utc)
                if now - reset_time >= timedelta(hours=1):
                    agent.posts_hour_reset = now
                    agent.posts_this_hour = 0

            is_spam = (agent.posts_this_hour or 0) >= settings.MAX_POSTS_PER_HOUR

            if own_session:
                await sess.commit()

            return is_spam

        except Exception:
            if own_session:
                await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()
