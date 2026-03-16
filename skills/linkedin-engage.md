---
name: linkedin-engage
description: Search LinkedIn posts where people are hiring or looking for candidates, and engage with relevant ones
trigger: When user asks to find LinkedIn hiring posts, or on scheduled scan cycle
---

# LinkedIn Engagement Skill

## What it does
Scans LinkedIn feed and search results for posts where people announce they're hiring, looking for candidates, or sharing job openings — then engages with relevant ones.

## Workflow

1. **Search hiring posts** — Use LinkedIn search with queries like:
   - `"we're hiring" AND "software engineer"`
   - `"looking for" AND "developer" AND "remote"`
   - `"open role" AND "backend"`
   - `#hiring #softwareengineer`
   - `"DM me" AND "engineer"`
2. **Filter by recency** — Only posts from last 48 hours
3. **AI Relevance Check** — Send post content to Claude, score relevance to profile (0-100)
4. **For relevant posts (score > 70)**:
   a. **Like the post** — Shows engagement
   b. **Draft a comment** — Claude generates a professional, non-spammy comment expressing interest
   c. **Optionally send DM** — If post says "DM me", draft a personalized message
5. **Store in DB** — Track all engaged posts to avoid duplicate engagement

## Comment Strategy
Comments should:
- Be 2-3 sentences max
- Reference something specific from the post
- Briefly mention relevant experience
- Express genuine interest
- NOT sound like a bot or template
- NOT include "I'm interested" spam

Example:
> "Great to see this role! I've been building distributed systems in Python for 4 years and would love to chat about the backend challenges you're tackling. Sent you a connection request."

## DM Strategy
- Only DM when the post explicitly invites DMs
- Keep it under 4 sentences
- Attach resume link (Google Drive/personal site) rather than file
- Mention the specific post you're responding to

## Rate Limits
- Max 10 comments per day
- Max 5 DMs per day
- Min 30-second gap between actions
- Randomize timing to appear human

## Database Schema
```sql
CREATE TABLE linkedin_engagements (
    id INTEGER PRIMARY KEY,
    post_url TEXT UNIQUE,
    author_name TEXT,
    post_content TEXT,
    relevance_score INTEGER,
    action_taken TEXT,  -- liked, commented, dm_sent, skipped
    comment_text TEXT,
    dm_text TEXT,
    engaged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
