# Users and sign-in

On the first start, Lynewright creates the bootstrap administrator when both `LYNEWRIGHT_BOOTSTRAP_EMAIL` and `LYNEWRIGHT_BOOTSTRAP_PASSWORD` are set. These values are only used to create that account; keep them out of source control and remove them from the runtime environment afterward.

Admins add, disable, and manage accounts in **Settings → Users**. Users can change their own sign-in email or password from **Account** in the account menu. A current password is required for either self-service change. Password changes and email changes sign out other sessions; changing an email does not send a verification message.

If an account is disabled, an administrator must re-enable it from **Settings → Users**.
