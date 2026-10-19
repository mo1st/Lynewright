import { useEffect, useState } from "react";
import { catalogueApi } from "../../api/catalogueClient";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import { SystemBar } from "./Feedback";
import styles from "./design-system.module.css";

const SECURITY_DOCS = "https://github.com/lynewright/lynewright/blob/main/docs/integrations/security.md";

export function SystemWarnings() {
  const [warnings, setWarnings] = useState<string[]>([]);
  useEffect(() => {
    let active = true;
    void Promise.allSettled([catalogueApi.get(), integrationManagementApi.list()]).then(([catalogue, integrations]) => {
      if (!active) return;
      const next: string[] = [];
      if (catalogue.status === "fulfilled" && catalogue.value.status.dev_key_trusted) next.push("Development catalogue key is trusted. Don't use this setup in production.");
      if (integrations.status === "fulfilled" && integrations.value.install_policy === "open") next.push("Outside integrations can be installed without confirmation. Review this setup before production.");
      setWarnings(next);
    });
    return () => { active = false; };
  }, []);
  if (!warnings.length) return null;
  return <SystemBar storageKey="security-and-install-policy" tone="warning" className={styles.systemWarningBar}>
    <span className={styles.systemWarningFull}>{warnings.join(" ")}</span><span className={styles.systemWarningCompact}>System notices need review.</span>{" "}<a href={SECURITY_DOCS} target="_blank" rel="noreferrer">Details</a>
  </SystemBar>;
}
