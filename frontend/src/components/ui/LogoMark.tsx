type LogoMarkProps = {
  size?: number;
  decorative?: boolean;
  label?: string;
};

export function LogoMark({ size = 28, decorative = false, label = "Lynewright logo mark" }: LogoMarkProps) {
  return <svg
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 56 56"
    width={size}
    height={size}
    fill="none"
    {...(decorative ? { "aria-hidden": "true" as const } : { role: "img" as const, "aria-label": label })}
  >
    <path d="M14 9V45H42" stroke="currentColor" strokeWidth="6" strokeLinecap="round" strokeLinejoin="round" />
    <circle cx="14" cy="9" r="6" fill="currentColor" />
    <circle cx="14" cy="27" r="6" fill="currentColor" />
    <circle cx="45" cy="45" r="8" fill="var(--color-accent)" />
  </svg>;
}
