---
name: Melisma Manual Implementer
description: Manually dispatched implementation agent that creates one draft pull request for a bounded Melisma task.

on:
  workflow_dispatch:
    inputs:
      task:
        description: Bounded implementation task
        required: true
        type: string
      acceptance_criteria:
        description: Independently authored acceptance criteria and required behavior
        required: true
        type: string
      test_command:
        description: Required deterministic test command, or NONE
        required: true
        default: NONE
        type: string
concurrency:
  job-discriminator: ${{ github.run_id }}
permissions:
  contents: read
  pull-requests: read
  issues: read
  copilot-requests: write

safe-outputs:
  create-pull-request:
    draft: true
    max: 1
    fallback-as-issue: false
    base-branch: main
    allowed-base-branches:
      - main
    allowed-branches:
      - agent/melisma-implementer-*
    title-prefix: "[implementer] "
    if-no-changes: error
---

# Melisma Manual Implementer

Implement the bounded Melisma task supplied by the human operator.

## Authoritative inputs

**Task**

${{ github.event.inputs.task }}

**Acceptance criteria**

${{ github.event.inputs.acceptance_criteria }}

**Required deterministic test command**

${{ github.event.inputs.test_command }}

Treat the supplied acceptance criteria as fixed input. Do not retrieve replacement criteria from an issue, pull request, comment, implementation summary, or other repository content.

## Trust boundary

You are the implementer, not the independent verifier.

You MUST NOT:

- merge a pull request
- push to `main`
- modify files under `.github/workflows/`
- modify files under `.github/skills/`
- modify `.github/aw/`
- modify `.gitattributes`
- modify independently authored acceptance tests merely to make them pass
- weaken existing tests or delete relevant assertions
- change the acceptance criteria
- claim independent verification
- approve your own pull request
- expand the work beyond the supplied task and acceptance criteria

Implementation summaries and test results are claims for later independent review. They are not independent proof of acceptance.

## Working method

1. Inspect the repository structure, relevant documentation, tests, and surrounding implementation before editing.
2. Identify the smallest coherent implementation that satisfies the supplied criteria.
3. Use test-first development where practical.
4. Preserve existing behavior outside the requested scope.
5. Add or update implementation-level tests only when necessary. Do not alter independently authored acceptance tests to accommodate the implementation.
6. Run the supplied deterministic test command exactly unless its value is `NONE`.
7. Run additional focused tests needed for the changed behavior.
8. Inspect the final diff for unrelated changes, generated files, credentials, temporary files, and debugging output.
9. Create exactly one draft pull request using the `create-pull-request` safe output.

## Pull request requirements

Use a source branch matching:

`agent/melisma-implementer-*`

The draft pull request description must contain:

- the supplied task
- the supplied acceptance criteria, unchanged
- a concise implementation summary
- every test command executed
- the exit status and concise result of each test
- any unverified behavior or environmental limitation
- any manual verification still required
- a statement that the pull request has not been independently verified

If the criteria cannot be implemented safely or required evidence cannot be obtained, do not invent success. Explain the blocker and avoid creating a misleading pull request.
