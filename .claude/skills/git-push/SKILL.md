---
name: git-push
description: Automatically commit all changes and push to GitHub. Use when the user says "commit", "push", "提交代码", "推送代码", or wants to save their work to GitHub.
argument-hint: "[commit message]"
disable-model-invocation: true
allowed-tools: Bash
---

# Auto Commit & Push to GitHub

Automatically stage, commit, and push all changes to the remote repository.

## Workflow

### Step 1: Check status

Run `git status` to see all changes (staged, unstaged, untracked). If there are no changes, inform the user and stop.

### Step 2: Review changes

Run `git diff --stat` and `git diff --staged --stat` to understand the scope of changes. Also run `git log --oneline -3` to see recent commit style.

### Step 3: Stage files

Stage all changed and new files. **IMPORTANT:**
- Do NOT stage files matching: `.env`, `*.key`, `*.pem`, `credentials*`, `*secret*`
- Do NOT stage large binary files or build artifacts
- If such files exist, warn the user and skip them
- Use `git add` with specific file paths instead of `git add -A` when sensitive files might exist

### Step 4: Generate commit message

If the user provided a commit message via `$ARGUMENTS`, use it directly.

If no message was provided, analyze the staged changes and generate a concise commit message:
- Follow the repository's existing commit message style (check `git log --oneline -5`)
- Keep the first line under 72 characters
- Use Chinese if the recent commits are in Chinese, otherwise use English
- Summarize the "why" not the "what"

### Step 5: Commit

Create the commit using a HEREDOC format:
```bash
git commit -m "$(cat <<'EOF'
Your commit message here

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

### Step 6: Push

- Determine the current branch: `git branch --show-current`
- Check if the branch has an upstream: `git rev-parse --abbrev-ref @{upstream} 2>/dev/null`
- If no upstream, push with `-u`: `git push -u origin <branch>`
- If upstream exists, push normally: `git push`
- **NEVER force push** unless the user explicitly asked for it
- If push fails due to remote changes, inform the user and suggest `git pull --rebase` first

### Step 7: Confirm

Show the user:
- The commit hash and message
- The branch and remote URL
- Number of files changed

## Safety Rules

- NEVER commit `.env`, secrets, credentials, or API keys
- NEVER force push to main/master
- NEVER use `--no-verify` to skip hooks
- If a pre-commit hook fails, fix the issue and create a NEW commit (don't amend)
- Always show the user what will be committed before committing
