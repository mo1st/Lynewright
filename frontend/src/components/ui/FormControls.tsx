import { forwardRef, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { ChevronDown } from "lucide-react";
import styles from "./design-system.module.css";

export function FormField({ label, htmlFor, helpText, error, required = false, children, className = "" }: { label: ReactNode; htmlFor?: string; helpText?: ReactNode; error?: ReactNode; required?: boolean; children: ReactNode; className?: string }) {
  return <div className={`${styles.field} ${className}`.trim()}>{htmlFor ? <label className={styles.fieldLabel} htmlFor={htmlFor}>{label}{required ? <span aria-hidden="true"> *</span> : null}</label> : <div className={styles.fieldLabel}>{label}{required ? <span aria-hidden="true"> *</span> : null}</div>}{children}{helpText ? <div className={styles.fieldHelp}>{helpText}</div> : null}{error ? <div className={styles.fieldError} role="alert">{error}</div> : null}</div>;
}

type InputProps = InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean };
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input({ invalid = false, className = "", ...props }, ref) {
  return <input {...props} ref={ref} aria-invalid={invalid || props["aria-invalid"] || undefined} className={`${styles.input} ui-input${invalid ? ` ${styles.invalid}` : ""} ${className}`.trim()} />;
});

type SelectProps = SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean };
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select({ invalid = false, className = "", children, ...props }, ref) {
  return <span className={styles.selectWrap}><select {...props} ref={ref} aria-invalid={invalid || props["aria-invalid"] || undefined} className={`${styles.select} ui-select${invalid ? ` ${styles.invalid}` : ""} ${className}`.trim()}>{children}</select>{props.multiple ? null : <ChevronDown className={styles.selectArrow} size={16} aria-hidden="true" />}</span>;
});

type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean };
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea({ invalid = false, className = "", ...props }, ref) {
  return <textarea {...props} ref={ref} aria-invalid={invalid || props["aria-invalid"] || undefined} className={`${styles.textarea} ui-textarea${invalid ? ` ${styles.invalid}` : ""} ${className}`.trim()} />;
});

export function Checkbox({ label, className = "", ...props }: Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & { label: ReactNode }) {
  return <label className={`${styles.choice} ui-choice ${className}`.trim()}><input {...props} type="checkbox" />{label}</label>;
}

export function Switch({ label, checked, onChange, disabled = false, name, className = "" }: { label: ReactNode; checked: boolean; onChange: (checked: boolean) => void; disabled?: boolean; name?: string; className?: string }) {
  return <label className={`${styles.choice} ui-choice ${className}`.trim()}><span>{label}</span><span className={`${styles.switch}${checked ? ` ${styles.switchOn}` : ""}`}><input type="checkbox" role="switch" name={name} checked={checked} disabled={disabled} onChange={(event) => onChange(event.currentTarget.checked)} /><span className={styles.switchThumb} aria-hidden="true" /></span></label>;
}
