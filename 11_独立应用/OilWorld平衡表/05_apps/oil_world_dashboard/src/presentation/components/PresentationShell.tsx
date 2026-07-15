import type { PointerEvent, ReactNode } from "react";

export function PresentationShell({
  children,
  controlsVisible,
  onEdgePointerMove,
}: {
  children: ReactNode;
  controlsVisible: boolean;
  onEdgePointerMove: (event: PointerEvent<HTMLElement>) => void;
}) {
  return (
    <main className="presentation-viewport" onPointerMove={onEdgePointerMove}>
      <div className={`presentation-canvas${controlsVisible ? " controls-visible" : ""}`} data-testid="presentation-canvas">
        {children}
      </div>
    </main>
  );
}
