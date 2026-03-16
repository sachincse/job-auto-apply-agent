---
name: auto-respond
description: Automatically read and respond to recruiter messages on LinkedIn and email with AI-drafted replies
trigger: When user asks to check messages, or on scheduled message-check cycle
---

# Auto-Respond Skill

## What it does
Monitors LinkedIn messages and email inbox for recruiter outreach, uses Claude AI to draft and optionally send replies, and tracks conversations.

## Workflow

1. **Check LinkedIn messages** — Playwright opens LinkedIn messaging, reads unread messages
2. **Check email** — IMAP connection reads unread emails matching recruiter patterns
3. **Classify each message** via Claude AI:
   - `job_opportunity` — Recruiter reaching out about a role
   - `interview_request` — Invitation to interview
   - `rejection` — Thanks but no thanks
   - `follow_up` — Follow-up on existing conversation
   - `spam` — Irrelevant / mass outreach
   - `other` — Doesn't fit categories above
4. **For job_opportunity messages**:
   a. Extract: company, role, salary (if mentioned), location, job description
   b. Score fit using Claude AI against profile
   c. If score > threshold:
      - Draft enthusiastic reply expressing interest
      - Include availability for a call
      - If auto-send enabled, send immediately
      - If not, save as draft for user review
   d. If score < threshold:
      - Draft polite decline
      - Save as draft (never auto-send declines)
5. **For interview_request messages**:
   - Draft confirmation reply with available time slots from `profile.yaml`
   - Always save as draft (never auto-send interview responses)
6. **For follow_up messages**:
   - Draft contextual reply based on conversation history in DB
7. **Store everything** in DB

## Reply Templates (via Jinja2)

### Interested Reply
```
Hi {{recruiter_name}},

Thank you for reaching out about the {{role}} position at {{company}}! This sounds like a great fit for my background in {{relevant_skills}}.

I'd love to learn more about the role and team. I'm available for a call {{availability}}.

Looking forward to connecting!

Best,
{{user_name}}
```

### Polite Decline
```
Hi {{recruiter_name}},

Thank you for thinking of me for the {{role}} position at {{company}}. I appreciate you reaching out.

At this time, I'm focused on opportunities more aligned with {{preferred_focus}}, but I'd love to stay connected for future opportunities.

Best regards,
{{user_name}}
```

## Configuration
```yaml
messaging:
  auto_send_interested: false    # true = send immediately, false = save as draft
  auto_send_decline: false       # always recommend false
  check_interval_minutes: 60
  availability: "weekdays between 10am-4pm EST"
  min_fit_score_to_reply: 50
```

## Database Schema
```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    source TEXT,           -- linkedin, email
    sender_name TEXT,
    sender_profile TEXT,
    message_content TEXT,
    classification TEXT,
    fit_score INTEGER,
    draft_reply TEXT,
    reply_sent BOOLEAN DEFAULT 0,
    sent_at TIMESTAMP,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
