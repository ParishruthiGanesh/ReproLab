"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type RunDetail } from "@/lib/api";
import { FinalReport } from "@/components/FinalReport";

export default function ExamplePage() {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .example()
      .then(setRun)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="notice bad">
        <strong>Cannot load the example report</strong>
        {error}
      </div>
    );
  }
  if (!run || !run.manifest) {
    return (
      <p className="muted">
        <span className="spinner" /> Loading…
      </p>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1>Example report</h1>
        <p>
          A worked illustration of ReproLab&apos;s output, so you can see the
          format before running anything. The run below never happened.
        </p>
      </div>
      <FinalReport runId={run.id} manifest={run.manifest} isExample />
      <p className="small muted" style={{ marginTop: "1.5rem" }}>
        <Link href="/">← Start a real run</Link>
      </p>
    </>
  );
}
