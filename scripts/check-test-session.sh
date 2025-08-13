#!/bin/bash
# Check for test user sessions that should not be committed

EXIT_CODE=0

for file in "$@"; do
    if grep -n "user_session_[0-9]" "$file"; then
        echo "❌ Test user session found in $file"
        echo "   Please remove or anonymize before committing!"
        EXIT_CODE=1
    fi
done

exit $EXIT_CODE
