import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

type Phase = "idle" | "closing" | "opening";

interface IrisTransitionContextValue {
  phase: Phase;
  transition: (swap: () => void) => void;
}

const IrisTransitionContext = createContext<IrisTransitionContextValue>({
  phase: "idle",
  transition: (swap) => swap(),
});

export function IrisTransitionProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const phaseRef = useRef<Phase>("idle");
  const onClosedRef = useRef<null | (() => void)>(null);

  const setPhaseState = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhase(next);
  }, []);

  const transition = useCallback(
    (swap: () => void) => {
      if (phaseRef.current !== "idle") {
        swap();
        return;
      }
      onClosedRef.current = swap;
      setPhaseState("closing");
    },
    [setPhaseState]
  );

  const handleEnd = useCallback(() => {
    if (phaseRef.current === "closing") {
      onClosedRef.current?.();
      onClosedRef.current = null;
      setPhaseState("opening");
      return;
    }
    if (phaseRef.current === "opening") {
      setPhaseState("idle");
    }
  }, [setPhaseState]);

  useEffect(() => {
    if (phase === "idle") return;
    const id = window.setTimeout(handleEnd, 760);
    return () => window.clearTimeout(id);
  }, [handleEnd, phase]);

  return (
    <IrisTransitionContext.Provider value={{ phase, transition }}>
      {children}
      <IrisOverlay phase={phase} onEnd={handleEnd} />
    </IrisTransitionContext.Provider>
  );
}

export function useIrisTransition() {
  return useContext(IrisTransitionContext);
}

function IrisOverlay({ phase, onEnd }: { phase: Phase; onEnd: () => void }) {
  if (phase === "idle") return null;
  return (
    <div
      aria-hidden
      className={`iris-wipe iris-wipe--${phase}`}
      onAnimationEnd={onEnd}
    />
  );
}
