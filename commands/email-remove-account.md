---
description: Remove an email account
allowed-tools: ["mcp__multi_mail__email_remove_account", "mcp__multi_mail__email_list_accounts"]
---

The user wants to remove an email account.

1. If no account id was specified in $ARGUMENTS, call `email_list_accounts` to
   show available accounts and ask which one to remove.
2. Confirm the removal with the user before proceeding — this cannot be undone.
3. Call `email_remove_account` with the confirmed account id.
4. Call `email_list_accounts` to show the updated list.
