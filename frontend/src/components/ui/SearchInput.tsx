export function SearchInput({ value, onChange, placeholder = "Search..." }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  return <input className="search-input" type="search" value={value} placeholder={placeholder} aria-label={placeholder} onChange={(event) => onChange(event.target.value)} />;
}
