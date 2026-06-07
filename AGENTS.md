# cc-connect Integration

This project is managed via cc-connect, a bridge to messaging platforms.

## Scheduled tasks (cron)

When the user asks you to do something on a schedule (e.g. "every day at 6am",
"every Monday morning"), use the Bash/shell tool to run:

  cc-connect cron add --cron "<min> <hour> <day> <month> <weekday>" --prompt "<task description>" --desc "<short label>"

Environment variables CC_PROJECT and CC_SESSION_KEY are already set; do not
specify --project or --session-key.

Examples:
  cc-connect cron add --cron "0 6 * * *" --prompt "Collect GitHub trending repos and send a summary" --desc "Daily GitHub Trending"
  cc-connect cron add --cron "0 9 * * 1" --prompt "Generate a weekly project status report" --desc "Weekly Report"

To list, run, edit, or delete cron jobs:
  cc-connect cron list
  cc-connect cron exec <job-id>
  cc-connect cron edit <job-id> <field> <value>
  cc-connect cron del <job-id>

Use `cron exec <job-id>` to run an existing scheduled task immediately; this is
different from the `--exec <command>` flag used when creating a shell-command
cron job. Use `cron edit` to modify a single field instead of
delete-and-recreate. Common editable fields: cron_expr, prompt, exec,
description, enabled (true/false), mute (true/false), timeout_mins (int).
Run `cc-connect cron edit --help` for the full field list.

Examples:
  cc-connect cron exec abc123
  cc-connect cron edit abc123 cron_expr "0 9 * * *"
  cc-connect cron edit abc123 enabled false
  cc-connect cron edit abc123 prompt "Updated daily summary task"

## Send message to current chat

To proactively send a message back to the user's chat session, use --stdin
heredoc for long or multi-line messages:

  cc-connect send --stdin <<'CCEOF'
  your message here
  CCEOF

For short single-line messages:

  cc-connect send -m "short message"
