import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The backend is a separate service; the browser talks to it directly at
  // NEXT_PUBLIC_API_BASE so SSE streaming is not buffered by a proxy hop.
  env: {
    NEXT_PUBLIC_API_BASE:
      process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000",
  },
};

export default config;
