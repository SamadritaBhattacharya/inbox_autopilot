"use client";

import { useEffect, useRef, useState } from "react";
import type { RunStatus } from "@/lib/types";

/**
 * The right pane: the live browser.
 *
 * Frames are painted straight to a canvas and **never enter React state**. They arrive
 * several times a second as base64 JPEG; routing megabytes through reconciliation would
 * thrash the whole tree to update one image. The subscription hands them to an `Image` and
 * draws — no re-render involved.
 */

type Props = {
  subscribeFrame: (handler: (jpegBase64: string) => void) => () => void;
  status: RunStatus;
  action?: string;
};

export function Viewport({ subscribeFrame, status, action }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [hasFrame, setHasFrame] = useState(false);

  useEffect(() => {
    const unsubscribe = subscribeFrame((jpegBase64) => {
      const canvas = canvasRef.current;
      if (!canvas) return;

      const image = new Image();
      image.onload = () => {
        // Match the backing store to the frame so the browser scales once, on paint,
        // instead of us resampling every frame in JS.
        if (canvas.width !== image.width || canvas.height !== image.height) {
          canvas.width = image.width;
          canvas.height = image.height;
        }
        canvas.getContext("2d")?.drawImage(image, 0, 0);
        setHasFrame(true);
      };
      image.src = `data:image/jpeg;base64,${jpegBase64}`;
    });
    return unsubscribe;
  }, [subscribeFrame]);

  return (
    <div className="relative flex h-full min-h-0 flex-col overflow-hidden rounded-[--radius-card] border border-line bg-surface">
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
        <span className="flex gap-1.5" aria-hidden>
          <i className="h-2 w-2 rounded-full bg-line2" />
          <i className="h-2 w-2 rounded-full bg-line2" />
          <i className="h-2 w-2 rounded-full bg-line2" />
        </span>
        <span className="ml-1 truncate font-mono text-[11px] text-faint">
          {action ? `${action}` : "live browser"}
        </span>
        {status === "running" && (
          <span className="ml-auto flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-muted">
            <i className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted" />
            live
          </span>
        )}
      </div>

      <div className="relative min-h-0 flex-1 bg-ink">
        <canvas ref={canvasRef} className="h-full w-full object-contain" />

        {!hasFrame && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center">
            <div className="h-10 w-10 rounded-[--radius-control] border border-line2" aria-hidden />
            <p className="max-w-xs text-[13px] leading-relaxed text-faint">
              {status === "idle"
                ? "The browser view appears here once a run starts."
                : "Waiting for the first frame — the agent is working."}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
