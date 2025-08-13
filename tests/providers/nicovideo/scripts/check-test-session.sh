#!/bin/bash
# Check for nicovideo test user sessions that should not be committed
# This script prevents accidental commits of real user session data in nicovideo provider tests

EXIT_CODE=0

for file in "$@"; do
    if grep -n "user_session_[0-9]" "$file"; then
        echo "❌ Nicovideo test user session found in $file"
        echo "   Please remove user session data before committing!"
        EXIT_CODE=1
    fi
done

exit $EXIT_CODE
