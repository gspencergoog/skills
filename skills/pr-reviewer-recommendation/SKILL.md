---
name: pr-reviewer-recommendation
description: Analyzes GitHub Pull Requests (PRs) or git branches to recommend the best primary and secondary reviewers. Uses git history, ownership, blame, and repository collaboration to identify the most suitable contributors. Use when asked to find, suggest, or recommend reviewers for a PR, branch, or commit range.
---

# PR Reviewer Recommendation Skill

This skill analyzes a GitHub Pull Request (PR), branch, or commit range to recommend the best candidates for code review. It identifies both **Primary suggestions** (who directly owns/wrote the modified lines or files) and **Secondary suggestions** (who owns the surrounding directories or has high domain expertise).

> [!IMPORTANT]
> **WRITING GUIDELINES**
> When presenting reviewer recommendations to the user, ensure the tone is professional, factual, and concise, adhering to the [natural-writing](../natural-writing/SKILL.md) standards.

## Workflow

When asked to find or recommend reviewers for a PR or branch:

1. **Locate the Repository**: Ensure you are in the target local git repository directory.
2. **Execute the Recommender Script**: Run the bundled Python script to perform the historical git and metadata analysis. Choose the command that matches the context:

   * **For a specific GitHub PR**:
     ```bash
     python3 ~/code/cheats/agents/skills/pr-reviewer-recommendation/scripts/recommend_reviewers.py --pr <pr-number>
     ```
   * **For a specific local/remote branch (comparing to default branch)**:
     ```bash
     python3 ~/code/cheats/agents/skills/pr-reviewer-recommendation/scripts/recommend_reviewers.py --branch <branch-name>
     ```
   * **For a custom commit range (e.g. comparing two commits or tags)**:
     ```bash
     python3 ~/code/cheats/agents/skills/pr-reviewer-recommendation/scripts/recommend_reviewers.py --compare <commit-range>
     ```
   * **For the current active branch**:
     ```bash
     python3 ~/code/cheats/agents/skills/pr-reviewer-recommendation/scripts/recommend_reviewers.py
     ```

3. **Incorporate Related Skills**:
   * If you need deeper understanding of the PR's purpose or discussion to refine suggestions (e.g. finding who commented on related issues), use the [analyze-github-pr](../analyze-github-pr/SKILL.md) skill.
   * To see precise semantic diffs and impact on structural entities, use the [sem-semantic-diff](../sem-semantic-diff/SKILL.md) skill.

4. **Formulate the Recommendation**:
   * **Primary suggestions**: Prioritize users who have directly authored or modified the changed files, ranked by commit count and recency (within the last 6 months).
   * **Secondary suggestions**: Identify users who have modified surrounding files in the parent directories, or who are prominent contributors to that specific domain.
   * **Format**: Present a clean markdown table showing the suggested reviewers, their match scores, historical commit count on those files, and contact info, followed by a copyable `gh pr edit` command to assign them.
