import { useEffect } from "react";

export function Overlay({
  children,
  onClose,
  label,
}: {
  children: React.ReactNode;
  onClose: () => void;
  label?: string;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={label}
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 50,
        background: "rgba(14,16,24,0.78)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 48,
        animation: "overlay-fade 0.18s ease-out",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="pixel-panel"
        style={{
          width: "min(880px, 100%)",
          maxHeight: "85vh",
          overflow: "auto",
          position: "relative",
          padding: 24,
          animation: "overlay-pop 0.18s ease-out",
        }}
      >
        <button
          className="pixel-btn text-[10px]"
          aria-label="Close"
          onClick={onClose}
          style={{ position: "absolute", top: 12, right: 12 }}
        >
          ✕
        </button>
        {label && (
          <h2
            className="font-hud"
            style={{ fontSize: 14, marginBottom: 16, color: "var(--ink)" }}
          >
            {label}
          </h2>
        )}
        {children}
      </div>
    </div>
  );
}
