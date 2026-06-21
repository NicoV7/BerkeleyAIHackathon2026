export function BattleOverlay({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 50,
        background: "var(--bg)",
        overflow: "auto",
      }}
    >
      {children}
    </div>
  );
}
