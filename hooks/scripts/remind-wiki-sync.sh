#!/usr/bin/env bash
# PostToolUse hook: after Edit/Write on omnilog/ source, nudge Claude to
# consider syncing the OmniLog wiki via the wiki-sync skill. Non-blocking.
set -euo pipefail

input="$(cat)"
file_path="$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(data.get("tool_input", {}).get("file_path", ""))
' 2>/dev/null || true)"

case "$file_path" in
  */omnilog/*.py)
    case "$file_path" in
      */tests/*|*/test_*.py|*_test.py)
        exit 0
        ;;
    esac
    printf '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "omnilog/ 소스가 수정되었습니다. 기능이 추가되거나 동작이 바뀌었다면, 커밋 전에 wiki-sync 스킬(omnilog MCP)로 관련 위키 문서를 등록/갱신하세요."}}'
    ;;
esac

exit 0
