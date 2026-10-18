import { useMemo, useState, type FormEvent } from "react";
import { Eye, EyeOff } from "lucide-react";
import type { CurrentUser } from "../api/client";
import { api, describeApiError } from "../api/client";
import { confirmAction } from "../context/ConfirmContext";
import { Badge, Button, FormField, Input, KeyValue, PageHeader, Section, useToast } from "../components/ui";

type Props = { user: CurrentUser; onSignOut: () => void; onUserUpdated: (email: string) => void };

export default function AccountPage({ user, onSignOut, onUserUpdated }: Props) {
  const { addToast } = useToast();
  const [email, setEmail] = useState(user.email);
  const [emailPassword, setEmailPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPasswords, setShowPasswords] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sessionError, setSessionError] = useState("");
  const passwordStrength = useMemo(() => {
    if (!newPassword) return "Enter a new password to see its strength.";
    const score = [newPassword.length >= 12, newPassword.length >= 16, /[a-z]/.test(newPassword) && /[A-Z]/.test(newPassword), /\d/.test(newPassword) && /[^A-Za-z0-9]/.test(newPassword)].filter(Boolean).length;
    return score >= 3 ? "Strong password" : score === 2 ? "Good password" : "Use at least 8 characters; a longer password is stronger.";
  }, [newPassword]);

  async function changeEmail(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const result = await api.changeMyEmail({ new_email: email, current_password: emailPassword });
      setEmail(result.email); onUserUpdated(result.email); setEmailPassword("");
      addToast({ tone: "success", message: "Email updated. No verification email was sent." });
    } catch (caught) { setError(describeApiError(caught, "Could not update your email.")); }
    finally { setBusy(false); }
  }

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    if (newPassword !== confirmPassword) { setError("The new passwords do not match."); setBusy(false); return; }
    try {
      await api.changeMyPassword({ current_password: currentPassword, new_password: newPassword });
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
      addToast({ tone: "success", message: "Password updated. Other sessions were signed out." });
    } catch (caught) { setError(describeApiError(caught, "Could not update your password.")); }
    finally { setBusy(false); }
  }

  async function signOutOthers() {
    setSessionError("");
    try { await api.logoutAll(true); addToast({ tone: "success", message: "Other sessions were signed out." }); }
    catch (caught) { setSessionError(describeApiError(caught, "Could not sign out other sessions.")); }
  }

  async function signOutEverywhere() {
    const confirmed = await confirmAction({ title: "Sign out everywhere?", message: "This signs out this device and every other active session.", confirmLabel: "Sign out everywhere" });
    if (!confirmed) return;
    setSessionError("");
    try { await api.logoutAll(); onSignOut(); }
    catch (caught) { setSessionError(describeApiError(caught, "Could not sign out everywhere.")); }
  }

  return <div className="account-page">
    <PageHeader title="Account" description="Manage your sign-in details and active sessions." />
    {error ? <p className="error-message" role="alert" aria-live="assertive">{error}</p> : null}
    <Section title="Your account" description="Your email is used to sign in." className="account-section">
      <KeyValue items={[{ label: "Email", value: user.email }, { label: "Role", value: <Badge tone="neutral">{user.role}</Badge> }]} />
    </Section>
    <Section title="Change email" description="Email changes take effect right away. Lynewright does not send verification emails." className="account-section">
      <form className="account-form" onSubmit={(event) => void changeEmail(event)}>
        <FormField label="New email" htmlFor="account-new-email" required><Input id="account-new-email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></FormField>
        <FormField label="Current password" htmlFor="account-email-current-password" required><Input id="account-email-current-password" type="password" autoComplete="current-password" required value={emailPassword} onChange={(event) => setEmailPassword(event.target.value)} /></FormField>
        <div className="account-actions"><Button variant="secondary" type="submit" busy={busy}>Save email</Button></div>
      </form>
    </Section>
    <Section title="Change password" description="Changing your password signs out your other active sessions." className="account-section">
      <form className="account-form" onSubmit={(event) => void changePassword(event)}>
        <FormField label="Current password" htmlFor="account-current-password" required><Input id="account-current-password" type={showPasswords ? "text" : "password"} autoComplete="current-password" required value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></FormField>
        <FormField label="New password" htmlFor="account-new-password" helpText={passwordStrength} required><Input id="account-new-password" type={showPasswords ? "text" : "password"} autoComplete="new-password" minLength={8} required value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></FormField>
        <FormField label="Confirm new password" htmlFor="account-confirm-password" required><Input id="account-confirm-password" type={showPasswords ? "text" : "password"} autoComplete="new-password" minLength={8} required value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} /></FormField>
        <div className="account-actions">
          <Button variant="ghost" type="button" onClick={() => setShowPasswords((value) => !value)} icon={showPasswords ? <EyeOff size={16} /> : <Eye size={16} />}>
            {showPasswords ? "Hide passwords" : "Show passwords"}
          </Button>
          <Button variant="primary" type="submit" busy={busy}>Change password</Button>
        </div>
      </form>
    </Section>
    <Section title="Active sessions" description="Sign out devices you no longer use." className="account-section">
      {sessionError ? <p className="error-message" role="alert">{sessionError}</p> : null}
      <div className="account-actions">
        <Button variant="secondary" onClick={() => void signOutOthers()}>Sign out everywhere else</Button>
        <Button variant="danger" onClick={() => void signOutEverywhere()}>Sign out everywhere</Button>
      </div>
    </Section>
  </div>;
}
