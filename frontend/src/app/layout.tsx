import type { Metadata } from "next";
import { Nav } from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "ReproLab — AI research engineering agent",
  description:
    "Prepares, runs and reports on a reproducibility check for a research paper and its public code, backed by actual execution evidence.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="masthead">
            <div className="masthead-inner">
              <a className="wordmark" href="/">
                Repro<span>Lab</span>
              </a>
              <p className="tagline">
                Reproducibility checks backed by real execution evidence
              </p>
              <Nav />
            </div>
          </header>
          <main>{children}</main>
          <footer>
            <div className="footer-inner">
              <span>
                NVIDIA Nemotron via Nebius Token Factory (inference) · Tavily
                (retrieval) · isolated container runner (execution)
              </span>
              <span>
                MIT licensed ·{" "}
                <a href="https://github.com/ParishruthiGanesh/ReproLab">
                  source
                </a>
              </span>
            </div>
          </footer>
        </div>
      </body>
    </html>
  );
}
