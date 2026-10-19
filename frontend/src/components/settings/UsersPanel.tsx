import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import type { ManagedUser } from "../../api/client";
import { api, describeApiError } from "../../api/client";
import { confirmAction } from "../../context/ConfirmContext";
import { Badge, Button, CopyField, Dialog, FormField, Input, Section, Select, Table, useToast } from "../ui";

type DialogKind = "create" | "role" | "password" | "email" | "delete" | null;
const roles = ["viewer", "editor", "admin"] as const;
const passwordAlphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%";

function generatePassword(): string {
  const values = crypto.getRandomValues(new Uint8Array(24));
  return Array.from(values, (value) => passwordAlphabet[value % passwordAlphabet.length]).join("");
}

function formatCreated(value: string): string {
  if (!value) return "—";
  const date = new Date(value.includes("T") ? value : `${value.replace(" ", "T")}Z`);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

export function UsersPanel({ currentUserId }: { currentUserId: number }) {
  const { addToast } = useToast();
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [target, setTarget] = useState<ManagedUser | null>(null);
  const [email, setEmail] = useState("");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [role, setRole] = useState("viewer");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [resetApplied, setResetApplied] = useState(false);
  const [busy, setBusy] = useState(false);

  const activeAdmins = useMemo(() => users.filter((user) => user.role === "admin" && user.is_active).length, [users]);
  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try { setUsers(await api.listUsers()); }
    catch (caught) { setError(describeApiError(caught, "Could not load users.")); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);

  function closeDialog() {
    setDialog(null); setTarget(null); setEmail(""); setDeleteConfirmation(""); setRole("viewer"); setPassword(""); setShowPassword(false); setResetApplied(false);
  }

  function openCreate() { setEmail(""); setRole("viewer"); setPassword(""); setDialog("create"); }
  function openRole(user: ManagedUser) { setTarget(user); setRole(user.role); setDialog("role"); }
  function openReset(user: ManagedUser) { setTarget(user); setPassword(generatePassword()); setResetApplied(false); setDialog("password"); }
  function openEmail(user: ManagedUser) { setTarget(user); setEmail(user.email); setDialog("email"); }

  async function submitDialog(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (dialog === "create") {
        await api.createUser({ email, password, role });
        closeDialog(); await reload(); addToast({ tone: "success", message: "User added." });
      } else if (dialog === "role" && target) {
        await api.updateUser(target.id, { role });
        closeDialog(); await reload(); addToast({ tone: "success", message: "User role updated." });
      } else if (dialog === "password" && target && !resetApplied) {
        await api.updateUser(target.id, { password });
        setResetApplied(true); await reload(); addToast({ tone: "success", message: "Password reset. Copy it now; it will not be shown again." });
      } else if (dialog === "email" && target) {
        await api.updateUser(target.id, { email });
        closeDialog(); await reload(); addToast({ tone: "success", message: "User email updated." });
      } else if (dialog === "delete" && target && deleteConfirmation === target.email) {
        await api.deleteUser(target.id);
        closeDialog(); await reload(); addToast({ tone: "success", message: "User deleted. Existing run history is kept." });
      }
    } catch (caught) { setError(describeApiError(caught, "Could not save this user.")); }
    finally { setBusy(false); }
  }

  async function toggleActive(user: ManagedUser) {
    const shouldDisable = user.is_active;
    if (shouldDisable && !(await confirmAction({ title: `Disable ${user.email}?`, message: "This account will no longer be able to sign in.", confirmLabel: "Disable account", tone: "danger" }))) return;
    setError("");
    try {
      await api.updateUser(user.id, { is_active: !user.is_active });
      await reload(); addToast({ tone: "success", message: shouldDisable ? "User disabled." : "User enabled." });
    } catch (caught) { setError(describeApiError(caught, "Could not update this account.")); }
  }

  function openDelete(user: ManagedUser) { setTarget(user); setDeleteConfirmation(""); setDialog("delete"); }

  const columns = [
    { key: "email", header: "Email", render: (user: ManagedUser) => <span className="managed-user-email"><strong>{user.email}</strong>{user.id === currentUserId ? <Badge tone="info">You</Badge> : null}</span> },
    { key: "role", header: "Role", render: (user: ManagedUser) => user.role },
    { key: "status", header: "Status", render: (user: ManagedUser) => <Badge tone={user.is_active ? "positive" : "neutral"}>{user.is_active ? "Active" : "Disabled"}</Badge> },
    { key: "created", header: "Created", render: (user: ManagedUser) => formatCreated(user.created_at) },
  ];

  return <div className="managed-users-page">
    <Section title="Users" description="Manage who can sign in and what they can do." actions={<Button variant="primary" onClick={openCreate}>Add user</Button>} className="managed-users-section">
    {error && !dialog ? <p className="error-message" role="alert">{error}</p> : null}
    <Table label="Users" rows={users} loading={loading} empty="No users yet." getRowKey={(user) => user.id} columns={columns} rowActions={(user) => {
      const protectAdminRole = user.role === "admin" && activeAdmins <= 1;
      const protectAdminStatus = user.role === "admin" && user.is_active && activeAdmins <= 1;
      const guardReason = "Add another active admin before changing this admin's role or status.";
      const cannotDelete = user.id === currentUserId || (user.role === "admin" && user.is_active && activeAdmins <= 1);
      const deleteReason = user.id === currentUserId ? "You cannot delete your own account." : guardReason;
      return <div className="managed-user-actions" aria-label={`Actions for ${user.email}`}>
        <Button variant="secondary" size="sm" disabled={protectAdminRole} title={protectAdminRole ? guardReason : undefined} aria-label={protectAdminRole ? `Change role for ${user.email} unavailable. ${guardReason}` : `Change role for ${user.email}`} onClick={() => openRole(user)}>Change role</Button>
        <Button variant="secondary" size="sm" onClick={() => openReset(user)}>Reset password</Button>
        <Button variant="secondary" size="sm" onClick={() => openEmail(user)}>Change email</Button>
        <Button variant={user.is_active ? "danger" : "secondary"} size="sm" disabled={protectAdminStatus} title={protectAdminStatus ? guardReason : undefined} aria-label={protectAdminStatus ? `Disable ${user.email} unavailable. ${guardReason}` : `${user.is_active ? "Disable" : "Enable"} ${user.email}`} onClick={() => void toggleActive(user)}>{user.is_active ? "Disable" : "Enable"}</Button>
        <Button variant="danger" size="sm" disabled={cannotDelete} title={cannotDelete ? deleteReason : undefined} aria-label={cannotDelete ? `Delete ${user.email} unavailable. ${deleteReason}` : `Delete ${user.email}`} onClick={() => openDelete(user)}>Delete</Button>
      </div>;
    }} />
    </Section>

    <Dialog open={dialog === "create"} title="Add user" description="Create a sign-in for someone on your team." onClose={closeDialog}>
      <form className="managed-user-form" onSubmit={(event) => void submitDialog(event)}>
        {error ? <p className="error-message" role="alert" aria-live="assertive">{error}</p> : null}
        <FormField label="Email" htmlFor="managed-user-email" required><Input id="managed-user-email" type="email" autoComplete="off" required value={email} onChange={(event) => setEmail(event.target.value)} /></FormField>
        <FormField label="Role" htmlFor="managed-user-role" required><Select id="managed-user-role" value={role} onChange={(event) => setRole(event.target.value)}>{roles.map((item) => <option key={item} value={item}>{item[0].toUpperCase() + item.slice(1)}</option>)}</Select></FormField>
        <FormField label="Initial password" htmlFor="managed-user-password" helpText="Use at least 12 characters. Share it securely; the user can change it in Account." required><Input id="managed-user-password" type={showPassword ? "text" : "password"} autoComplete="new-password" minLength={12} required value={password} onChange={(event) => setPassword(event.target.value)} /></FormField>
        <div className="managed-user-actions">
          <Button variant="secondary" type="button" onClick={() => { setPassword(generatePassword()); setShowPassword(true); }}>Generate password</Button>
          <Button variant="ghost" type="button" onClick={() => setShowPassword((value) => !value)}>{showPassword ? "Hide password" : "Show password"}</Button>
        </div>
        {showPassword && password ? <CopyField label="Generated initial password — copy it now" value={password} /> : null}
        <footer className="dialog-actions"><Button variant="primary" type="submit" busy={busy}>Add user</Button><Button variant="secondary" type="button" onClick={closeDialog}>Cancel</Button></footer>
      </form>
    </Dialog>

    <Dialog open={dialog === "role" && Boolean(target)} title={`Change role for ${target?.email ?? "user"}`} onClose={closeDialog}>
      <form className="managed-user-form" onSubmit={(event) => void submitDialog(event)}>
        {error ? <p className="error-message" role="alert">{error}</p> : null}
        <FormField label="Role" htmlFor="managed-user-role-edit" required><Select id="managed-user-role-edit" value={role} onChange={(event) => setRole(event.target.value)}>{roles.map((item) => <option key={item} value={item}>{item[0].toUpperCase() + item.slice(1)}</option>)}</Select></FormField>
        <footer className="dialog-actions"><Button variant="primary" type="submit" busy={busy}>Save role</Button><Button variant="secondary" type="button" onClick={closeDialog}>Cancel</Button></footer>
      </form>
    </Dialog>

    <Dialog open={dialog === "password" && Boolean(target)} title={`Reset password for ${target?.email ?? "user"}`} description="This generated password is shown once. Copy it before closing this dialog." onClose={closeDialog}>
      <form className="managed-user-form" onSubmit={(event) => void submitDialog(event)}>
        {error ? <p className="error-message" role="alert">{error}</p> : null}
        <p className="muted-text">The reset revokes all of this user's existing sessions.</p>
        {password ? <CopyField label="One-time password" value={password} /> : null}
        <footer className="dialog-actions">
          {resetApplied ? <Button variant="secondary" type="button" onClick={closeDialog}>Done</Button> : <><Button variant="primary" type="submit" busy={busy}>Reset password</Button><Button variant="secondary" type="button" onClick={closeDialog}>Cancel</Button></>}
        </footer>
      </form>
    </Dialog>

    <Dialog open={dialog === "email" && Boolean(target)} title={`Change email for ${target?.email ?? "user"}`} onClose={closeDialog}>
      <form className="managed-user-form" onSubmit={(event) => void submitDialog(event)}>
        {error ? <p className="error-message" role="alert">{error}</p> : null}
        <FormField label="New email" htmlFor="managed-user-email-edit" required><Input id="managed-user-email-edit" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></FormField>
        <p className="muted-text">All sessions for this user will be signed out.</p>
        <footer className="dialog-actions"><Button variant="primary" type="submit" busy={busy}>Save email</Button><Button variant="secondary" type="button" onClick={closeDialog}>Cancel</Button></footer>
      </form>
    </Dialog>

    <Dialog open={dialog === "delete" && Boolean(target)} title={`Delete ${target?.email ?? "user"}?`} description="This removes their sign-in and revokes their sessions. Existing run history is kept." onClose={closeDialog}>
      <form className="managed-user-form" onSubmit={(event) => void submitDialog(event)}>
        {error ? <p className="error-message" role="alert">{error}</p> : null}
        <p>This cannot be undone. Type the user's email to confirm.</p>
        <FormField label="Email to confirm" htmlFor="managed-user-delete-confirm" required><Input id="managed-user-delete-confirm" autoComplete="off" required value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} /></FormField>
        <footer className="dialog-actions"><Button variant="danger" type="submit" busy={busy} disabled={deleteConfirmation !== target?.email}>Delete user</Button><Button variant="secondary" type="button" onClick={closeDialog}>Cancel</Button></footer>
      </form>
    </Dialog>
  </div>;
}
