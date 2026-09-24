import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async redirects() {
    return [{ source: "/", destination: "/today", permanent: false }];
  },
  // The API's session cookie only works same-site, which in local development means
  // opening the app at 127.0.0.1 (matching the API's 127.0.0.1:8000) rather than
  // localhost. Next's dev server otherwise blocks cross-origin dev requests (HMR,
  // etc.) from that host. See docs/ARCHITECTURE.md.
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
