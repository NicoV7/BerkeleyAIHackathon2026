import { useEffect, useState } from "react";

export function useTypewriter(text: string, active: boolean, speed = 18): string {
  const [shown, setShown] = useState(active ? "" : text);
  useEffect(() => {
    if (!active) {
      setShown(text);
      return;
    }
    setShown("");
    let i = 0;
    const id = setInterval(() => {
      i++;
      setShown(text.slice(0, i));
      if (i >= text.length) clearInterval(id);
    }, speed);
    return () => clearInterval(id);
  }, [text, active, speed]);
  return shown;
}
