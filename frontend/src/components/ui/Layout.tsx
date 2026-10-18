import type { ReactNode } from "react";
import styles from "./design-system.module.css";

export function AppShell({ sidebar, children, systemBar, preview = false, className = "" }: { sidebar: ReactNode; children: ReactNode; systemBar?: ReactNode; preview?: boolean; className?: string }) {
  return <div className={`${styles.shell}${preview ? ` ${styles.shellPreview}` : ""} ${className}`.trim()}>{sidebar}<div className={styles.shellMain}>{systemBar}{children}</div></div>;
}

export function Sidebar({ children, header, footer, label = "Main navigation", className = "", navClassName = "" }: { children: ReactNode; header?: ReactNode; footer?: ReactNode; label?: string; className?: string; navClassName?: string }) {
  return <aside className={`${styles.sidebar} ${className}`.trim()} aria-label={label}>{header ? <div className={styles.sidebarHeader}>{header}</div> : null}<nav className={`${styles.sidebarNav} ${navClassName}`.trim()} aria-label={label}>{children}</nav>{footer ? <div className={styles.sidebarFooter}>{footer}</div> : null}</aside>;
}

export function NavItem({ href, active = false, icon, badge, children, onClick, className = "" }: { href?: string; active?: boolean; icon?: ReactNode; badge?: ReactNode; children: ReactNode; onClick?: () => void; className?: string }) {
  const classes = `${styles.navItem} ui-nav-item${active ? ` ${styles.navItemActive}` : ""} ${className}`.trim();
  const content = <>{icon ? <span className={styles.navIcon} aria-hidden="true">{icon}</span> : null}<span className={styles.navLabel}>{children}</span>{badge ? <span className={styles.navBadge}>{badge}</span> : null}</>;
  if (onClick) return <button type="button" className={classes} aria-current={active ? "page" : undefined} onClick={onClick}>{content}</button>;
  return <a className={classes} href={href ?? "#"} aria-current={active ? "page" : undefined}>{content}</a>;
}

export function PageHeader({ title, description, primaryAction, secondaryAction, className = "" }: { title: ReactNode; description?: ReactNode; primaryAction?: ReactNode; secondaryAction?: ReactNode; className?: string }) {
  return <header className={`${styles.pageHeader} ${className}`.trim()}><div className={styles.pageHeaderCopy}><h1 className={styles.pageTitle}>{title}</h1>{description ? <p className={styles.pageDescription}>{description}</p> : null}</div>{primaryAction || secondaryAction ? <div className={styles.pageActions}>{secondaryAction}{primaryAction}</div> : null}</header>;
}

export function Section({ title, description, actions, children, headingLevel = 2, className = "" }: { title?: ReactNode; description?: ReactNode; actions?: ReactNode; children: ReactNode; headingLevel?: 2 | 3 | 4; className?: string }) {
  const Heading = `h${headingLevel}` as const;
  return <section className={`${styles.section} ${className}`.trim()}>{title || actions ? <header className={styles.sectionHeader}>{title ? <div><Heading className={styles.sectionTitle}>{title}</Heading>{description ? <p className={styles.sectionDescription}>{description}</p> : null}</div> : null}{actions}</header> : null}{children}</section>;
}

export function Card({ children, padded = true, as = "div", className = "" }: { children: ReactNode; padded?: boolean; as?: "div" | "article" | "section"; className?: string }) {
  const Component = as;
  return <Component className={`${styles.card}${padded ? ` ${styles.cardPadded}` : ""} ${className}`.trim()}>{children}</Component>;
}
