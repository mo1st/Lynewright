import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import styles from "./design-system.module.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";
type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant | "quiet"; size?: ButtonSize; busy?: boolean; icon?: ReactNode; dangerConfirm?: boolean };
const variantClass = { primary: styles.buttonPrimary, secondary: styles.buttonSecondary, ghost: styles.buttonGhost, quiet: styles.buttonGhost, danger: styles.buttonDanger };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", busy = false, icon, dangerConfirm = false, className = "", children, disabled, type = "button", ...props },
  ref,
) {
  const normalizedVariant = variant === "quiet" ? "ghost" : variant;
  return <button type={type} {...props} ref={ref} data-button-variant={normalizedVariant} data-button-size={size} className={`${styles.button} ${variantClass[variant]} ${size === "sm" ? styles.buttonSm : styles.buttonMd} ui-button ui-button--${variant === "quiet" ? "quiet" : variant} ui-button--${size}${dangerConfirm && variant === "danger" ? ` ${styles.buttonDangerConfirm} ui-button--danger-confirm` : ""} ${className}`.trim()} aria-busy={busy || undefined} disabled={disabled || busy}>
    {busy ? <span className={styles.spinner} aria-hidden="true" /> : icon}
    <span>{children}</span>
  </button>;
});

type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & { label: string; children: ReactNode };
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({ label, className = "", children, type = "button", ...props }, ref) {
  return <button type={type} {...props} ref={ref} className={`${styles.iconButton} ui-icon-button ui-icon-button--ghost ${className}`.trim()} aria-label={label} title={label}>{children}</button>;
});
