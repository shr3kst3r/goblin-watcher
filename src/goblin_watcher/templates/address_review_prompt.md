Address the review feedback on this pull request.

{ticket_id}: {title}

{repos_block}

{description}

{addition_block}{review_block}

Work through every item above. Adjudicate before you edit: check each one
against the code as it stands now, fix what is genuinely wrong, and for
anything you are not changing, say what it was and why you are leaving it.
Outdated threads and bot findings get the same treatment — the comment is a
claim about the code, not a verdict on it.

Re-run whatever was failing until it passes locally, and run this project's
own verification commands before you call it done.

Do not reply to, resolve, or otherwise write to the review threads on GitHub,
and do not comment on the Linear ticket or the GitHub issue. Report here, in
this session, what you changed and what you deliberately left alone.

When the fixes are ready, push them with `gw pr open` — the PR already exists,
so it pushes the branch and skips creating a second one.
{focus}
