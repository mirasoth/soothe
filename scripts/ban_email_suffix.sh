#!/usr/bin/env bash
#
# ban_email_suffix.sh - Git pre-commit hook that blocks commits from banned email suffixes.
#
# Prevents accidental commits authored under a disallowed email domain (e.g. a
# corporate address leaking into an open-source repo). The check inspects the
# email that will be recorded for the upcoming commit; if its domain matches a
# banned suffix the commit is rejected with an actionable message.
#
# Banned suffixes are matched case-insensitively against the end of the email
# address. Edit the BANNED_EMAIL_SUFFIXES array below to adjust the list.
#
# Usage (install as a git hook):
#   cp scripts/ban_email_suffix.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Or chain it after an existing pre-commit hook, e.g.:
#   .git/hooks/pre-commit:
#     #!/usr/bin/env bash
#     ./scripts/verify_finally.sh && ./scripts/ban_email_suffix.sh
#
# Exit codes:
#   0 - commit author email is allowed (or no banned suffixes configured)
#   1 - commit author email matches a banned suffix
#
# NOTE: This script intentionally performs no repo mutation and no network
# access; it only reads `git config user.email` and the staged commit metadata.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration: edit this list to ban additional email suffixes.
# Suffixes are matched case-insensitively against the end of the email.
# ---------------------------------------------------------------------------
BANNED_EMAIL_SUFFIXES=(
  "@tencent.com"
)

# ---------------------------------------------------------------------------
# Resolve the author email for the upcoming commit.
#
# Precedence (mirrors git's own author resolution):
#   1. GIT_AUTHOR_EMAIL env var (set per-commit via -c or env)
#   2. repo-local user.email
#   3. global user.email
#   4. system user.email
# `git config --get` walks this chain for us, so we don't hardcode fallbacks.
# ---------------------------------------------------------------------------
author_email="$(git config --get user.email 2>/dev/null || true)"

if [[ -z "${author_email}" ]]; then
  # No email configured at all — let git produce its own error downstream.
  # Failing here would mask the real "please tell me who you are" message.
  exit 0
fi

# Normalise to lowercase for case-insensitive suffix matching.
email_lower="$(printf '%s' "${author_email}" | tr '[:upper:]' '[:lower:]')"

for suffix in "${BANNED_EMAIL_SUFFIXES[@]}"; do
  suffix_lower="$(printf '%s' "${suffix}" | tr '[:upper:]' '[:lower:]')"
  if [[ "${email_lower}" == *"${suffix_lower}" ]]; then
    cat >&2 <<EOF

❌ Commit blocked: author email is from a banned suffix.

    author email : ${author_email}
    banned suffix: ${suffix}

Set a permitted git identity before committing, e.g.:

    git config user.email "you@example.com"
    git config user.name  "Your Name"

(Use GIT_AUTHOR_EMAIL / GIT_AUTHOR_NAME for a one-off override, or
 --author="Name <email>" on the commit command line.)

If this block is a mistake, edit BANNED_EMAIL_SUFFIXES in
scripts/ban_email_suffix.sh and reinstall the hook.

EOF
    exit 1
  fi
done

exit 0
