---
name: report
description: Generate and send daily/weekly job hunting reports via Email and Telegram
trigger: When user asks for a report, or on scheduled report cycle (daily at 8pm)
---

# Reporting Skill

## What it does
Aggregates all job hunting activity into a clean report and delivers it via Email and/or Telegram.

## Report Contents

### Daily Report
```
📊 Job Hunt Report — {{date}}

🔍 SEARCH SUMMARY
• Jobs found today: {{jobs_found}}
• Avg fit score: {{avg_score}}
• Top platforms: {{top_platforms}}

✅ APPLICATIONS
• Applied today: {{applied_count}}
• Total applied (all time): {{total_applied}}
• Success rate: {{response_rate}}%

💬 LINKEDIN ENGAGEMENT
• Posts engaged: {{posts_engaged}}
• Comments made: {{comments_count}}
• DMs sent: {{dms_sent}}

📨 MESSAGES
• New messages received: {{new_messages}}
• Replies sent: {{replies_sent}}
• Pending drafts for review: {{pending_drafts}}

🏆 TOP MATCHES TODAY
{{#each top_jobs}}
• {{title}} @ {{company}} — Score: {{fit_score}} — {{status}}
{{/each}}

⚠️ ACTION NEEDED
{{#each pending_actions}}
• {{action_description}}
{{/each}}
```

### Weekly Summary (sent Sunday evening)
- Week-over-week trends
- Best performing platforms
- Response rate by company size
- Suggestions to improve (AI-generated)

## Delivery Channels

### Telegram
- Uses `python-telegram-bot` library
- Sends formatted message with markdown
- Supports inline buttons for quick actions (approve drafts, skip jobs)

### Email
- Uses SMTP (Gmail, Outlook, or custom)
- HTML formatted report using `templates/report.jinja`
- Attaches CSV of all jobs found (optional)

## Configuration
```yaml
reporting:
  channels:
    telegram: true
    email: true
  schedule:
    daily: "20:00"      # 8 PM daily report
    weekly: "SUN 20:00"  # Sunday weekly summary
  email:
    recipient: "your-email@example.com"
    include_csv: true
  telegram:
    send_instant_alerts: true  # Notify immediately when high-score job found
    alert_threshold: 90        # Only alert for 90+ score jobs
```

## Instant Alerts (Telegram)
When a job scores 90+ during a search cycle, immediately send a Telegram alert:
```
🚨 HIGH MATCH JOB ALERT

💼 Senior Backend Engineer @ TechCorp
📍 Remote | $150k-$180k
🎯 Fit Score: 95/100

🔗 [Apply Now](job_url)

Shall I auto-apply? Reply /yes or /no
```
