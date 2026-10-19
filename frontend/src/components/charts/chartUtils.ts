import { useEffect, useRef, useState } from "react";

export type ChartPoint = { label: string; value: number };
export const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
export const extent = (values: number[]) => [Math.min(...values, 0), Math.max(...values, 1)] as const;
export const scaleY = (value: number, min: number, max: number, height: number, padding = 20) => height - padding - ((value - min) / Math.max(max - min, 1)) * (height - padding * 2);
export const scaleX = (index: number, length: number, width: number, padding = 28) => padding + (index / Math.max(length - 1, 1)) * (width - padding * 2);

export function useMeasuredWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    const measure = () => setWidth(Math.max(ref.current?.clientWidth ?? 640, 240));
    measure(); if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure); if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}
