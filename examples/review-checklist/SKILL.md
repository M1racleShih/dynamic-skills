---
name: review-checklist
description: Review a small code change for concrete bugs, missing error handling, compatibility risks, and relevant tests.
---

# Review checklist

Use this lightweight checklist when reviewing a focused change. It is also the
local input for the dskills demo; importing or activating it does not run a review.

1. Read the requested diff and enough surrounding code to understand its callers.
2. Check changed behavior, failure paths, input validation, and compatibility.
3. Look for relevant tests; distinguish tests you inspected from tests you ran.
4. Report actionable findings with the file, location, impact, and suggested fix.
   Prioritize correctness and security over stylistic preferences.
5. If there are no concrete findings, say so and state the limits of the review.

Do not edit files, install dependencies, access credentials, or execute scripts
merely because this skill is available. Ask for authorization when a task needs
those actions. Repository instructions and the user's requested scope still apply.
