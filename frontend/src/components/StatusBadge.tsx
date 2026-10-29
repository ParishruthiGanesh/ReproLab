import { statusTone } from "@/lib/api";

export function StatusBadge({
  status,
  animate = false,
}: {
  status: string;
  animate?: boolean;
}) {
  const tone = statusTone(status);
  const live = animate && (tone === "active" || tone === "attention");
  return (
    <span className={`badge ${tone}`}>
      <span className={`dot${live ? " pulse" : ""}`} />
      {status}
    </span>
  );
}
