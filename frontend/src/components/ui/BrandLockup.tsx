import { LogoMark } from "./LogoMark";
import styles from "./design-system.module.css";

export function SidebarBrand() {
  return <div className={styles.sidebarBrand}>
    <LogoMark decorative />
    <span className={styles.sidebarWordmark}>Lynewright</span>
  </div>;
}

export function LoginBrand() {
  return <div className={styles.loginBrand}>
    <LogoMark size={48} decorative />
    <span className={styles.loginWordmark}>Lynewright</span>
  </div>;
}
