import { Button } from "../ui";
import { useEffect, useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { Mutate } from "./sectionTypes";

type Mode = "restricted" | "custom" | "full";
type Props = {
  integrationId: string;
  requested: string[];
  permissionMode: string;
  granted: string[];
  manual?: boolean;
  integrationEnabled?: boolean;
  busy: boolean;
  mutate: Mutate;
};

const modeValue = (value: string): Mode => (["restricted", "custom", "full"].includes(value) ? value : "restricted") as Mode;

export default function AuthorizationSection({ integrationId, requested, permissionMode, granted, manual = false, integrationEnabled = false, busy, mutate }: Props) {
  const [mode, setMode] = useState<Mode>(modeValue(permissionMode));
  const [capabilities, setCapabilities] = useState(granted);
  const [declared, setDeclared] = useState(requested);
  const [options, setOptions] = useState<string[]>([]);
  const [optionsFailed, setOptionsFailed] = useState(false);

  useEffect(() => {
    setMode(modeValue(permissionMode));
    setCapabilities(granted);
    setDeclared(requested);
  }, [permissionMode, granted, requested]);

  useEffect(() => {
    if (!manual) return;
    let active = true;
    void integrationManagementApi.manualCapabilities().then((result) => {
      if (active) {
        setOptions(result.items);
        setOptionsFailed(false);
      }
    }).catch(() => {
      if (active) setOptionsFailed(true);
    });
    return () => { active = false; };
  }, [manual]);

  const toggleGrant = (capability: string) => setCapabilities((current) => current.includes(capability) ? current.filter((item) => item !== capability) : [...current, capability]);
  const toggleDeclaration = (capability: string) => {
    setDeclared((current) => current.includes(capability) ? current.filter((item) => item !== capability) : [...current, capability]);
    setCapabilities((current) => current.filter((item) => item !== capability));
  };

  return <section className="integration-subsection" aria-label="Integration authorization">
    <header className="panel-header"><div><h4>Authorization</h4><p className="muted-text">Declare what a manual adapter may request, then grant only what it needs. A mapping cannot broaden these grants.</p></div></header>
    <div className="form-grid">
      {manual ? <fieldset className="form-grid__wide"><legend>Declared capabilities</legend>
        <p className="muted-text">Disable the integration to change this operator-authored declaration. Declaring a capability does not grant it.</p>
        {optionsFailed ? <p className="form-error" role="alert">Capability options could not be loaded. Existing declarations are unchanged.</p> : options.map((capability) => {
          const requiredBaseline = capability === "network:use" || (capability === "credentials:use" && requested.includes(capability));
          return <label key={capability} className="checkbox-row" htmlFor={`integration-declaration-${capability.replace(/[^A-Za-z0-9_-]/g, "-")}`}><input id={`integration-declaration-${capability.replace(/[^A-Za-z0-9_-]/g, "-")}`} autoComplete="off" type="checkbox" checked={declared.includes(capability)} disabled={busy || integrationEnabled || requiredBaseline} onChange={() => toggleDeclaration(capability)} /> {capability}{requiredBaseline ? " · required by this connection" : ""}</label>;
        })}
      </fieldset> : null}
      <label htmlFor="integration-permission-mode">Permission mode<select id="integration-permission-mode" name="permission-mode" autoComplete="off" value={mode} disabled={busy} onChange={(event) => setMode(event.target.value as Mode)}><option value="restricted">Restricted</option><option value="custom">Custom grants</option><option value="full">Full access</option></select></label>
      {mode !== "full" ? <fieldset><legend>Granted capabilities</legend>{declared.length ? declared.map((capability) => <label key={capability} className="checkbox-row" htmlFor={`integration-capability-${capability.replace(/[^A-Za-z0-9_-]/g, "-")}`}><input id={`integration-capability-${capability.replace(/[^A-Za-z0-9_-]/g, "-")}`} name={`capability-${capability}`} autoComplete="off" type="checkbox" checked={capabilities.includes(capability)} disabled={busy} onChange={() => toggleGrant(capability)} /> {capability}</label>) : <p className="muted-text">No capabilities were declared by this integration.</p>}</fieldset> : <p className="muted-text"><strong>Full access (host policy)</strong> — the host-defined full-access policy is active; no custom grant list is displayed.</p>}
      <Button variant="secondary" type="button" disabled={busy || (manual && !integrationEnabled && optionsFailed)} onClick={() => void mutate("authorization", () => integrationManagementApi.updateAuthorization(integrationId, { permission_mode: mode, granted_capabilities: mode === "full" ? [] : capabilities, ...(manual && !integrationEnabled ? { requested_capabilities: declared } : {}) }), "Authorization updated. The integration must be re-enabled before runtime use.")}>Save authorization</Button>
    </div>
  </section>;
}
